from __future__ import annotations

import ipaddress
import logging
import socket
import ssl
import typing
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

import anyio
import httpx
from httpcore import (
    SOCKET_OPTION,
    AsyncNetworkStream,
    ConnectError,
    ConnectTimeout,
)
from httpcore._backends.anyio import AnyIOBackend, AnyIOStream

from forge.research.models import RetrievedDocument

logger = logging.getLogger("forge.research.fetcher")

DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 10.0
DEFAULT_USER_AGENT = "Forge-Research/0.1 (+https://github.com/Rufidatul726/forge)"

ALLOWED_SCHEMES = {"http", "https"}

ALLOWED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "application/json",
    "text/json",
}

DISALLOWED_DOMAINS = {
    "localhost",
    "localhost.localdomain",
    "broadcasthost",
    "metadata.google.internal",
    "instance-data",
    "metadata",
}

DISALLOWED_DOMAIN_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".corp",
    ".intra",
    ".private",
    ".test",
    ".invalid",
    ".example",
)

# Known cloud metadata and hypervisor service IP addresses
CLOUD_METADATA_IPS = {
    "169.254.169.254",  # AWS, GCP, Azure IMDS, OpenStack, DigitalOcean
    "168.63.129.16",    # Azure WireServer / IMDS
    "100.100.100.200",  # Alibaba Cloud metadata
}

SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}


class FetcherError(Exception):
    """Base error for URL retrieval."""

    def __init__(self, message: str, public_message: str | None = None) -> None:
        super().__init__(message)
        self.public_message = public_message or message


class SSRFSecurityError(FetcherError):
    """Raised when URL resolves to private, loopback, or disallowed address."""

    def __init__(self, message: str = "Destination address is prohibited") -> None:
        super().__init__(
            message,
            public_message="Destination address is prohibited (private or reserved address)",
        )


class ContentTooLargeError(FetcherError):
    """Raised when downloaded response exceeds maximum size limit."""

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(
            f"Content length {size} exceeds limit of {limit} bytes",
            public_message=f"Document exceeds maximum size limit of {limit // 1024} KB",
        )


class UnsupportedContentTypeError(FetcherError):
    """Raised when server returns unsupported content type (e.g. binary)."""

    def __init__(self, content_type: str) -> None:
        super().__init__(
            f"Unsupported content type: {content_type}",
            public_message=f"Unsupported document content type: {content_type}",
        )


class TooManyRedirectsError(FetcherError):
    """Raised when redirect limit is exceeded."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"Exceeded redirect limit of {limit}",
            public_message=f"Too many redirects (exceeded limit of {limit})",
        )


class RedirectLoopError(TooManyRedirectsError):
    """Raised when a redirect loop / cycle is detected."""

    def __init__(self, target_url: str) -> None:
        super().__init__(DEFAULT_MAX_REDIRECTS)
        self.public_message = "Redirect loop detected"


class URLValidationError(FetcherError):
    """Raised when URL is malformed or uses disallowed scheme."""


def is_prohibited_ip(ip_str: str) -> bool:
    """Check if IP address is private, loopback, link-local, multicast, or reserved."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True

    # Cloud metadata endpoints
    if str(ip) in CLOUD_METADATA_IPS:
        return True

    # Standard loopback, private, reserved, link_local, multicast, unspecified
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_reserved
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
    ):
        return True

    # Check global reachability
    if not ip.is_global:
        return True

    # IPv6 checks
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return is_prohibited_ip(str(ip.ipv4_mapped))
        # NAT64 prefixes (64:ff9b::/96 and 64:ff9b:1::/48)
        if ip in ipaddress.ip_network("64:ff9b::/96") or ip in ipaddress.ip_network("64:ff9b:1::/48"):
            return True
        # Documentation prefix (2001:db8::/32)
        if ip in ipaddress.ip_network("2001:db8::/32"):
            return True
        # Site-local (fec0::/10)
        if ip in ipaddress.ip_network("fec0::/10"):
            return True
        # 6to4 (2002::/16)
        if ip in ipaddress.ip_network("2002::/16"):
            return True

    # IPv4 extra reserved/special ranges
    if isinstance(ip, ipaddress.IPv4Address):
        # Current network "this host" (0.0.0.0/8)
        if ip in ipaddress.ip_network("0.0.0.0/8"):
            return True
        # Shared Address Space / CGNAT (100.64.0.0/10)
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return True
        # Benchmarking (198.18.0.0/15)
        if ip in ipaddress.ip_network("198.18.0.0/15"):
            return True
        # Documentation ranges (TEST-NET-1, 2, 3)
        if (
            ip in ipaddress.ip_network("192.0.2.0/24")
            or ip in ipaddress.ip_network("198.51.100.0/24")
            or ip in ipaddress.ip_network("203.0.113.0/24")
        ):
            return True
        # Reserved / Future use (240.0.0.0/4)
        if ip in ipaddress.ip_network("240.0.0.0/4"):
            return True

    return False


def _parse_numeric_or_unusual_ip(hostname: str) -> str | None:
    """
    Detect and normalize non-standard IP representations (e.g. decimal integers,
    octal, hex, or abbreviated IPv4) into standard dotted-decimal or raise URLValidationError.
    """
    clean_host = hostname.strip().strip("[]")

    # Pure decimal integer representation (e.g. "2130706433" -> 127.0.0.1)
    if clean_host.isdigit():
        try:
            val = int(clean_host)
            if 0 <= val <= 0xFFFFFFFF:
                return str(ipaddress.IPv4Address(val))
        except (ValueError, OverflowError):
            raise URLValidationError(f"Invalid integer IP representation: {hostname!r}")

    # Check for dotted notation with unusual parts or abbreviated notation (2, 3, 4 parts)
    parts = clean_host.split(".")
    if len(parts) in (2, 3, 4) and all(p for p in parts):
        parsed_parts: list[int] = []
        is_numeric_dotted = True
        for p in parts:
            if p.startswith(("0x", "0X")):
                try:
                    parsed_parts.append(int(p, 16))
                except ValueError:
                    is_numeric_dotted = False
                    break
            elif p.startswith("0") and len(p) > 1 and p.isdigit():
                try:
                    parsed_parts.append(int(p, 8))
                except ValueError:
                    is_numeric_dotted = False
                    break
            elif p.isdigit():
                try:
                    parsed_parts.append(int(p, 10))
                except ValueError:
                    is_numeric_dotted = False
                    break
            else:
                is_numeric_dotted = False
                break

        if is_numeric_dotted and len(parsed_parts) == len(parts):
            try:
                if len(parsed_parts) == 4 and all(0 <= x <= 255 for x in parsed_parts):
                    return ".".join(str(x) for x in parsed_parts)
                elif len(parsed_parts) == 3 and 0 <= parsed_parts[0] <= 255 and 0 <= parsed_parts[1] <= 255 and 0 <= parsed_parts[2] <= 65535:
                    val = (parsed_parts[0] << 24) | (parsed_parts[1] << 16) | parsed_parts[2]
                    return str(ipaddress.IPv4Address(val))
                elif len(parsed_parts) == 2 and 0 <= parsed_parts[0] <= 255 and 0 <= parsed_parts[1] <= 16777215:
                    val = (parsed_parts[0] << 24) | parsed_parts[1]
                    return str(ipaddress.IPv4Address(val))
            except (ValueError, OverflowError):
                pass
            raise URLValidationError(f"Disallowed non-standard IP representation: {hostname!r}")

    return None


def is_prohibited_hostname(hostname: str) -> bool:
    """Check if hostname resolves to local/internal naming convention or prohibited IP."""
    h = hostname.lower().strip().rstrip(".")
    if not h:
        return True

    if h in DISALLOWED_DOMAINS:
        return True

    if any(h.endswith(suffix) for suffix in DISALLOWED_DOMAIN_SUFFIXES):
        return True

    # Check non-standard numeric IP
    try:
        norm_ip = _parse_numeric_or_unusual_ip(h)
        if norm_ip is not None and is_prohibited_ip(norm_ip):
            return True
    except URLValidationError:
        return True

    return False


def safe_resolve_hostname(hostname: str, port: int = 80) -> list[str]:
    """Resolve hostname and ensure ALL returned IP addresses are public and safe."""
    clean_host = hostname.strip().strip("[]").rstrip(".")
    if not clean_host:
        raise URLValidationError("Hostname cannot be empty.")

    if is_prohibited_hostname(clean_host):
        raise SSRFSecurityError("Destination address is prohibited (local or reserved domain)")

    # Check if host is already an IP address
    norm_ip = _parse_numeric_or_unusual_ip(clean_host)
    check_ip_str = norm_ip or clean_host
    try:
        ip = ipaddress.ip_address(check_ip_str)
        if is_prohibited_ip(str(ip)):
            raise SSRFSecurityError("Destination address is prohibited (private, loopback, or reserved IP)")
        return [str(ip)]
    except ValueError:
        pass

    try:
        addr_infos = socket.getaddrinfo(clean_host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetcherError(
            f"DNS resolution failed for {clean_host}",
            public_message="Could not resolve host name",
        ) from exc

    resolved_ips: list[str] = []
    for family, _, _, _, sockaddr in addr_infos:
        ip_addr = sockaddr[0]
        if is_prohibited_ip(ip_addr):
            raise SSRFSecurityError("Destination address is prohibited (private, loopback, or reserved IP)")
        resolved_ips.append(ip_addr)

    if not resolved_ips:
        raise FetcherError(
            f"No IP addresses found for {clean_host}",
            public_message="Could not resolve host name",
        )

    return resolved_ips


class SafeNetworkBackend(AnyIOBackend):
    """
    Network backend that enforces safe IP resolution and prevents DNS rebinding (TOCTOU) attacks.

    Why this custom transport/backend is necessary:
    Standard HTTP clients resolve the hostname, check the resulting IP, and then pass the
    hostname to the operating system's socket connect API. In a DNS rebinding attack, the
    attacker's authoritative DNS server returns a legitimate public IP during the pre-check,
    but immediately responds with a private IP (e.g. 127.0.0.1 or 169.254.169.254) when the
    subsequent connect() lookup occurs.

    SafeNetworkBackend eliminates this time-of-check/time-of-use window by:
    1. Resolving and validating all destination IPs against strict SSRF rules before connection.
    2. Establishing the TCP stream directly to the pre-validated IP address.
    3. Preserving the original hostname for the TLS handshake (SNI and certificate validation),
       ensuring full TLS security without repeating an untrusted DNS resolution.
    """

    def __init__(self, dns_resolver: Callable[[str, int], list[str]] = safe_resolve_hostname) -> None:
        super().__init__()
        self._dns_resolver = dns_resolver

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        resolved_ips = self._dns_resolver(host, port)
        target_ip = resolved_ips[0]
        if is_prohibited_ip(target_ip):
            raise SSRFSecurityError("Destination address is prohibited (private, loopback, or reserved IP)")

        # Connect directly to pre-validated IP to eliminate DNS rebinding window
        return await super().connect_tcp(
            target_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class SafeHTTPTransport(httpx.AsyncHTTPTransport):
    """
    Specialized AsyncHTTPTransport configured with SafeNetworkBackend for DNS pinning
    and zero keepalive connection pooling to prevent pooled connection reuse to unsafe destinations.
    Preserves TLS verification and SNI validation.
    """

    def __init__(
        self,
        *,
        dns_resolver: Callable[[str, int], list[str]] = safe_resolve_hostname,
        verify: ssl.SSLContext | str | bool = True,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(verify=verify, http2=False, **kwargs)
        import httpcore

        ssl_context = httpx.create_ssl_context(verify=verify)
        self._safe_backend = SafeNetworkBackend(dns_resolver=dns_resolver)
        # Configure pool with safe network backend and max_keepalive_connections=0
        # so every request connection validates destination IP afresh.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            network_backend=self._safe_backend,
            max_connections=50,
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
        )


def validate_url(url: str) -> tuple[str, str, int]:
    """Validate scheme, host, and port for safety. Return normalized (scheme, host, port)."""
    clean_url = url.strip()
    if not clean_url:
        raise URLValidationError("URL cannot be empty.")

    # Reject whitespace or control characters in URL
    if any(c.isspace() or ord(c) < 32 for c in clean_url):
        raise URLValidationError("URL contains invalid whitespace or control characters.")

    parsed = urlsplit(clean_url)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise URLValidationError(
            f"Unsupported URL scheme: {parsed.scheme!r}. Only HTTP and HTTPS are permitted."
        )

    hostname = parsed.hostname
    if not hostname:
        raise URLValidationError("URL must contain a valid host name.")

    if parsed.username or parsed.password:
        raise URLValidationError("URLs containing embedded credentials are not allowed.")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise URLValidationError("URL contains an invalid port number.") from exc

    if not (1 <= port <= 65535):
        raise URLValidationError(f"URL port {port} is out of valid range (1-65535).")

    return scheme, hostname, port


def is_content_type_allowed(content_type_header: str | None) -> bool:
    """Check if Content-Type is among allowed documentation/text MIME types."""
    if not content_type_header:
        # Default assume HTML if unspecified
        return True
    mime = content_type_header.split(";")[0].strip().lower()
    return mime in ALLOWED_CONTENT_TYPES


class SafeDocumentFetcher:
    """
    Secure, bounded HTTP client for web research and documentation retrieval.
    Enforces bounded streaming, SSRF prevention, DNS pinning, TLS verification,
    and cross-origin sensitive header striping.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
        dns_resolver: Callable[[str, int], list[str]] = safe_resolve_hostname,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.max_redirects = max_redirects
        self.max_response_bytes = max_response_bytes
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.dns_resolver = dns_resolver
        self._custom_client = client

    def _create_client(self) -> httpx.AsyncClient:
        if self._custom_client is not None:
            return self._custom_client

        transport = SafeHTTPTransport(dns_resolver=self.dns_resolver, verify=True)
        return httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                connect=self.connect_timeout,
                read=self.read_timeout,
                write=5.0,
                pool=5.0,
            ),
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,text/plain,text/markdown,application/json;q=0.9,*/*;q=0.1",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=False,
            verify=True,
        )

    async def fetch(self, url: str) -> tuple[str, str, str]:
        """
        Safely fetch raw content from a URL with full SSRF, redirect, bounded streaming,
        and response size protection.
        Returns: (final_url, content_type, text_content).
        """
        current_url = url.strip()
        orig_scheme, orig_host, orig_port = validate_url(current_url)
        orig_origin = (orig_scheme, orig_host.lower(), orig_port)

        redirect_count = 0
        visited_urls: set[str] = {current_url}
        client = self._create_client()

        # Track any custom headers across requests
        current_headers: dict[str, str] = {}

        try:
            while True:
                scheme, host, port = validate_url(current_url)
                current_origin = (scheme, host.lower(), port)

                # Verify host against SSRF upfront as fast reject before connection attempt
                self.dns_resolver(host, port)

                # Cross-origin redirect protection: strip sensitive headers and adjust Referer
                request_headers = dict(current_headers)
                if current_origin != orig_origin:
                    for h_name in list(request_headers.keys()):
                        if h_name.lower() in SENSITIVE_HEADERS:
                            del request_headers[h_name]

                    # Referrer policy: no-referrer-when-downgrade and origin-only for cross-origin
                    if orig_scheme == "https" and scheme == "http":
                        request_headers.pop("Referer", None)
                    else:
                        request_headers["Referer"] = f"{orig_scheme}://{orig_host}/"

                try:
                    request = client.build_request("GET", current_url, headers=request_headers)
                    response = await client.send(request, stream=True)
                except httpx.TimeoutException as exc:
                    raise FetcherError(
                        f"Request timed out connecting to {host}",
                        public_message="Connection timed out",
                    ) from exc
                except (httpx.ConnectError, ConnectError) as exc:
                    # Check for SSL/TLS verification failure
                    err_str = str(exc)
                    if "certificate verify failed" in err_str or "CERTIFICATE_VERIFY_FAILED" in err_str:
                        raise FetcherError(
                            f"TLS certificate verification failed for {host}: {exc}",
                            public_message="TLS certificate verification failed",
                        ) from exc
                    raise FetcherError(
                        f"Connection failed to {host}: {exc}",
                        public_message="Could not establish secure connection",
                    ) from exc
                except httpx.HTTPError as exc:
                    raise FetcherError(
                        f"HTTP request error: {exc}",
                        public_message="HTTP request failed",
                    ) from exc

                try:
                    # 1. Handle redirects
                    if response.is_redirect:
                        await response.aclose()
                        redirect_count += 1
                        if redirect_count > self.max_redirects:
                            raise TooManyRedirectsError(self.max_redirects)

                        location = response.headers.get("location")
                        if not location:
                            raise FetcherError(
                                "Redirect response missing Location header",
                                public_message="Invalid redirect response",
                            )

                        target_url = urljoin(current_url, location)
                        if target_url in visited_urls:
                            raise RedirectLoopError(target_url)
                        visited_urls.add(target_url)

                        # Validate redirect destination URL & SSRF early
                        r_scheme, r_host, r_port = validate_url(target_url)
                        self.dns_resolver(r_host, r_port)

                        current_url = target_url
                        continue

                    # 2. Not a redirect - validate status
                    if response.status_code >= 400:
                        await response.aclose()
                        raise FetcherError(
                            f"Server returned HTTP error status {response.status_code}",
                            public_message=f"Document retrieval failed with status {response.status_code}",
                        )

                    # 3. Validate content type BEFORE reading body
                    content_type = response.headers.get("content-type", "text/html")
                    if not is_content_type_allowed(content_type):
                        await response.aclose()
                        raise UnsupportedContentTypeError(content_type)

                    # 4. Check Content-Length if present as early rejection
                    content_length_header = response.headers.get("content-length")
                    if content_length_header:
                        try:
                            content_length = int(content_length_header)
                            if content_length > self.max_response_bytes:
                                await response.aclose()
                                raise ContentTooLargeError(content_length, self.max_response_bytes)
                        except ValueError:
                            # Malformed content-length header; fall through to bounded streaming
                            pass

                    # 5. Bounded streaming read: enforce limit on actual bytes delivered
                    chunks: list[bytes] = []
                    total_bytes = 0

                    try:
                        async for chunk in response.aiter_bytes():
                            total_bytes += len(chunk)
                            if total_bytes > self.max_response_bytes:
                                await response.aclose()
                                raise ContentTooLargeError(total_bytes, self.max_response_bytes)
                            chunks.append(chunk)
                    except (httpx.ReadTimeout, httpx.ReadError) as exc:
                        await response.aclose()
                        raise FetcherError(
                            f"Connection interrupted while reading from {host}: {exc}",
                            public_message="Connection interrupted while downloading document",
                        ) from exc

                    body_bytes = b"".join(chunks)

                    # Decode text using response encoding or utf-8 fallback
                    encoding = response.encoding or "utf-8"
                    try:
                        text_content = body_bytes.decode(encoding, errors="replace")
                    except Exception:
                        text_content = body_bytes.decode("utf-8", errors="replace")

                    return current_url, content_type, text_content

                finally:
                    await response.aclose()

        finally:
            if self._custom_client is None:
                await client.aclose()
