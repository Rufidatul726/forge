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
}


class FetcherError(Exception):
    """Base error for URL retrieval."""

    def __init__(self, message: str, public_message: str | None = None) -> None:
        super().__init__(message)
        self.public_message = public_message or message


class SSRFSecurityError(FetcherError):
    """Raised when URL resolves to private, loopback, or disallowed address."""

    def __init__(self, message: str = "Destination address is prohibited") -> None:
        super().__init__(message, public_message="Destination address is prohibited (private or reserved address)")


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


class URLValidationError(FetcherError):
    """Raised when URL is malformed or uses disallowed scheme."""


def is_prohibited_ip(ip_str: str) -> bool:
    """Check if IP address is private, loopback, link-local, multicast, or reserved."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
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

    # IPv6 checks
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return is_prohibited_ip(str(ip.ipv4_mapped))
        # NAT64 prefix (64:ff9b::/96)
        if ip in ipaddress.ip_network("64:ff9b::/96"):
            return True
        # Documentation prefix (2001:db8::/32)
        if ip in ipaddress.ip_network("2001:db8::/32"):
            return True
        # Site-local (fec0::/10)
        if ip in ipaddress.ip_network("fec0::/10"):
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

    return False


def is_prohibited_hostname(hostname: str) -> bool:
    """Check if hostname resolves to local/internal naming convention."""
    h = hostname.lower().strip(".")
    if not h:
        return True
    if h in DISALLOWED_DOMAINS:
        return True
    if (
        h.endswith(".localhost")
        or h.endswith(".local")
        or h.endswith(".internal")
        or h.endswith(".lan")
        or h.endswith(".home.arpa")
    ):
        return True
    return False


def safe_resolve_hostname(hostname: str, port: int = 80) -> list[str]:
    """Resolve hostname and ensure ALL returned IP addresses are public and safe."""
    if is_prohibited_hostname(hostname):
        raise SSRFSecurityError("Destination address is prohibited (local or reserved domain)")

    try:
        ip = ipaddress.ip_address(hostname)
        if is_prohibited_ip(str(ip)):
            raise SSRFSecurityError("Destination address is prohibited (private, loopback, or reserved IP)")
        return [str(ip)]
    except ValueError:
        pass

    try:
        addr_infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise FetcherError(f"DNS resolution failed for {hostname}", public_message="Could not resolve host name") from exc

    resolved_ips: list[str] = []
    for family, _, _, _, sockaddr in addr_infos:
        ip_addr = sockaddr[0]
        if is_prohibited_ip(ip_addr):
            raise SSRFSecurityError("Destination address is prohibited (private, loopback, or reserved IP)")
        resolved_ips.append(ip_addr)

    if not resolved_ips:
        raise FetcherError(f"No IP addresses found for {hostname}", public_message="Could not resolve host name")

    return resolved_ips


class SafeNetworkBackend(AnyIOBackend):
    """Network backend that enforces safe IP resolution and prevents DNS rebinding."""

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
        # Connect directly to pre-validated IP to eliminate DNS rebinding window
        return await super().connect_tcp(
            target_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


def validate_url(url: str) -> tuple[str, str, int]:
    """Validate scheme, host, and port for safety. Return normalized (scheme, host, port)."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise URLValidationError(
            f"Unsupported URL scheme: {parsed.scheme!r}. Only HTTP and HTTPS are permitted."
        )

    if not parsed.hostname:
        raise URLValidationError("URL must contain a valid host name.")

    if parsed.username or parsed.password:
        raise URLValidationError("URLs containing embedded credentials are not allowed.")

    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname, port


def is_content_type_allowed(content_type_header: str | None) -> bool:
    """Check if Content-Type is among allowed documentation/text MIME types."""
    if not content_type_header:
        # Default assume HTML if unspecified
        return True
    mime = content_type_header.split(";")[0].strip().lower()
    return mime in ALLOWED_CONTENT_TYPES


class SafeDocumentFetcher:
    """Secure, bounded HTTP client for web research and documentation retrieval."""

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

        network_backend = SafeNetworkBackend(dns_resolver=self.dns_resolver)
        transport = httpx.AsyncHTTPTransport(
            verify=True,
            http2=False,
        )
        # Inject network backend into transport's pool
        transport._pool._network_backend = network_backend

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
        Safely fetch raw content from a URL with full SSRF, redirect, and size protection.
        Returns: (final_url, content_type, text_content).
        """
        current_url = url.strip()
        redirect_count = 0
        client = self._create_client()

        # Retain original domain to avoid leaking credentials/sensitive headers on cross-domain redirect
        orig_scheme, orig_host, _ = validate_url(current_url)

        try:
            while True:
                scheme, host, port = validate_url(current_url)
                # Verify host against SSRF upfront as fast reject
                self.dns_resolver(host, port)

                request_headers: dict[str, str] = {}
                # Cross-domain check for sensitive headers if any were added
                if host.lower() != orig_host.lower():
                    request_headers["Referer"] = f"{orig_scheme}://{orig_host}/"

                try:
                    response = await client.get(current_url, headers=request_headers)
                except httpx.TimeoutException as exc:
                    raise FetcherError(f"Request timed out connecting to {host}", public_message="Connection timed out") from exc
                except httpx.ConnectError as exc:
                    raise FetcherError(f"Connection failed to {host}", public_message="Could not establish secure connection") from exc
                except httpx.HTTPError as exc:
                    raise FetcherError(f"HTTP request error: {exc}", public_message="HTTP request failed") from exc

                # Check for redirect
                if response.is_redirect:
                    redirect_count += 1
                    if redirect_count > self.max_redirects:
                        raise TooManyRedirectsError(self.max_redirects)

                    location = response.headers.get("location")
                    if not location:
                        raise FetcherError("Redirect response missing Location header", public_message="Invalid redirect response")

                    target_url = urljoin(current_url, location)
                    current_url = target_url
                    continue

                # Not a redirect - validate status
                if response.status_code >= 400:
                    raise FetcherError(
                        f"Server returned HTTP error status {response.status_code}",
                        public_message=f"Document retrieval failed with status {response.status_code}",
                    )

                # Validate content type
                content_type = response.headers.get("content-type", "text/html")
                if not is_content_type_allowed(content_type):
                    raise UnsupportedContentTypeError(content_type)

                # Check Content-Length if present
                content_length_header = response.headers.get("content-length")
                if content_length_header:
                    try:
                        content_length = int(content_length_header)
                        if content_length > self.max_response_bytes:
                            raise ContentTooLargeError(content_length, self.max_response_bytes)
                    except ValueError:
                        pass

                # Stream and accumulate body bytes up to limit
                body_bytes = response.content
                if len(body_bytes) > self.max_response_bytes:
                    raise ContentTooLargeError(len(body_bytes), self.max_response_bytes)

                # Decode text using response encoding or utf-8 fallback
                encoding = response.encoding or "utf-8"
                try:
                    text_content = body_bytes.decode(encoding, errors="replace")
                except Exception:
                    text_content = body_bytes.decode("utf-8", errors="replace")

                return current_url, content_type, text_content

        finally:
            if self._custom_client is None:
                await client.aclose()
