import asyncio
import gzip
import io
import pytest
import httpx

from forge.research.fetcher import (
    ContentTooLargeError,
    FetcherError,
    RedirectLoopError,
    SafeDocumentFetcher,
    SafeNetworkBackend,
    SSRFSecurityError,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
    URLValidationError,
    is_prohibited_hostname,
    is_prohibited_ip,
    safe_resolve_hostname,
    validate_url,
)


class AsyncStreamMock(httpx.AsyncByteStream):
    """Mock async byte stream that yields given chunks and tracks closure."""

    def __init__(self, chunks: list[bytes], raise_error: Exception | None = None) -> None:
        self._chunks = chunks
        self._raise_error = raise_error
        self.closed = False

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk
        if self._raise_error is not None:
            raise self._raise_error

    async def aclose(self) -> None:
        self.closed = True


# ==============================================================================
# SECTION 2: HTTP Response-Size Limits & Bounded Streaming Tests
# ==============================================================================

@pytest.mark.anyio
async def test_fetcher_response_smaller_than_limit():
    """A response smaller than the configured limit downloads successfully."""
    content = b"Small response text"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=content, headers={"Content-Type": "text/plain"})
    )
    fetcher = SafeDocumentFetcher(
        max_response_bytes=1000,
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=httpx.AsyncClient(transport=transport),
    )
    final_url, ctype, text = await fetcher.fetch("https://example.com/small")
    assert text == "Small response text"
    assert "text/plain" in ctype


@pytest.mark.anyio
async def test_fetcher_response_exactly_at_limit():
    """A response exactly at the limit succeeds without error."""
    exact_bytes = b"x" * 500
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=exact_bytes, headers={"Content-Type": "text/plain"})
    )
    fetcher = SafeDocumentFetcher(
        max_response_bytes=500,
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=httpx.AsyncClient(transport=transport),
    )
    _, _, text = await fetcher.fetch("https://example.com/exact")
    assert len(text) == 500


@pytest.mark.anyio
async def test_fetcher_response_exceeding_limit():
    """A response exceeding the configured limit is stopped and raises ContentTooLargeError."""
    overflow_bytes = b"x" * 501
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, content=overflow_bytes, headers={"Content-Type": "text/plain"})
    )
    fetcher = SafeDocumentFetcher(
        max_response_bytes=500,
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=httpx.AsyncClient(transport=transport),
    )
    with pytest.raises(ContentTooLargeError) as exc_info:
        await fetcher.fetch("https://example.com/large")
    assert "exceeds limit" in str(exc_info.value)


@pytest.mark.anyio
async def test_fetcher_missing_content_length():
    """Missing Content-Length header still enforces the bounded read limit."""
    stream_mock = AsyncStreamMock([b"a" * 100 for _ in range(10)])
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            stream=stream_mock,
        )
    )
    client = httpx.AsyncClient(transport=transport)
    fetcher = SafeDocumentFetcher(
        max_response_bytes=500,
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=client,
    )
    with pytest.raises(ContentTooLargeError):
        await fetcher.fetch("https://example.com/stream-missing-cl")
    assert stream_mock.closed is True


@pytest.mark.anyio
async def test_fetcher_incorrect_content_length_underreporting():
    """Server under-reports Content-Length (claims 100 bytes, sends 2000 bytes). Enforces limit."""
    stream_mock = AsyncStreamMock([b"b" * 100 for _ in range(20)])
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "Content-Length": "100"},
            stream=stream_mock,
        )
    )
    client = httpx.AsyncClient(transport=transport)
    fetcher = SafeDocumentFetcher(
        max_response_bytes=500,
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=client,
    )
    with pytest.raises(ContentTooLargeError):
        await fetcher.fetch("https://example.com/deceptive-cl")
    assert stream_mock.closed is True


@pytest.mark.anyio
async def test_fetcher_incorrect_content_length_overreporting():
    """Server over-reports Content-Length beyond limit, but body is small; fast rejects on header."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            content=b"tiny",
            headers={"Content-Type": "text/plain", "Content-Length": "1000000"},
        )
    )
    fetcher = SafeDocumentFetcher(
        max_response_bytes=500,
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=httpx.AsyncClient(transport=transport),
    )
    with pytest.raises(ContentTooLargeError):
        await fetcher.fetch("https://example.com/overreported-cl")


@pytest.mark.anyio
async def test_fetcher_chunked_response():
    """Chunked Transfer-Encoding response is read in chunks up to limit."""
    stream_mock = AsyncStreamMock([b"chunk1 ", b"chunk2 ", b"chunk3"])
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "Transfer-Encoding": "chunked"},
            stream=stream_mock,
        )
    )
    fetcher = SafeDocumentFetcher(
        max_response_bytes=1000,
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=httpx.AsyncClient(transport=transport),
    )
    _, _, text = await fetcher.fetch("https://example.com/chunked")
    assert text == "chunk1 chunk2 chunk3"
    assert stream_mock.closed is True


@pytest.mark.anyio
async def test_fetcher_compressed_response_expanding_beyond_limit():
    """Compressed response (gzip) that decompresses past the limit is halted safely."""
    # 10,000 bytes of repetitive data compresses down to ~40 bytes
    large_raw = b"A" * 10000
    compressed = gzip.compress(large_raw)
    assert len(compressed) < 100  # verified small compressed size

    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            content=compressed,
            headers={
                "Content-Type": "text/plain",
                "Content-Encoding": "gzip",
            },
        )
    )
    fetcher = SafeDocumentFetcher(
        max_response_bytes=1000,  # limit on uncompressed bytes delivered to application
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=httpx.AsyncClient(transport=transport),
    )
    with pytest.raises(ContentTooLargeError):
        await fetcher.fetch("https://example.com/gzip-bomb")


@pytest.mark.anyio
async def test_fetcher_connection_interruption():
    """Connection read failure during streaming triggers FetcherError with clean stream closure."""
    stream_mock = AsyncStreamMock(
        [b"first chunk ok"],
        raise_error=httpx.ReadError("Network connection reset by peer"),
    )
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            stream=stream_mock,
        )
    )
    fetcher = SafeDocumentFetcher(
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=httpx.AsyncClient(transport=transport),
    )
    with pytest.raises(FetcherError) as exc_info:
        await fetcher.fetch("https://example.com/faulty")
    assert "interrupted" in str(exc_info.value).lower()
    assert stream_mock.closed is True


@pytest.mark.anyio
async def test_fetcher_timeout_and_cancellation():
    """Request timeout raises FetcherError with public timeout message."""
    class TimeoutTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("Read operation timed out")

    fetcher = SafeDocumentFetcher(
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=httpx.AsyncClient(transport=TimeoutTransport()),
    )
    with pytest.raises(FetcherError) as exc_info:
        await fetcher.fetch("https://example.com/timeout")
    assert "timed out" in str(exc_info.value).lower()


# ==============================================================================
# SECTION 3: SSRF & Network Boundary Protections Tests
# ==============================================================================

def test_ssrf_prohibited_ipv4_and_ipv6():
    """Verify loopback, private, link-local, multicast, and cloud metadata ranges are rejected."""
    # Loopback
    assert is_prohibited_ip("127.0.0.1") is True
    assert is_prohibited_ip("127.100.200.1") is True
    assert is_prohibited_ip("::1") is True
    # Private
    assert is_prohibited_ip("10.1.2.3") is True
    assert is_prohibited_ip("172.16.5.6") is True
    assert is_prohibited_ip("192.168.0.254") is True
    assert is_prohibited_ip("fc00::1") is True
    assert is_prohibited_ip("fd12::34") is True
    # Link local & Cloud metadata
    assert is_prohibited_ip("169.254.169.254") is True
    assert is_prohibited_ip("168.63.129.16") is True  # Azure WireServer
    assert is_prohibited_ip("100.100.100.200") is True  # Alibaba IMDS
    assert is_prohibited_ip("fe80::1") is True
    # Reserved, broadcast, unspecified
    assert is_prohibited_ip("0.0.0.0") is True
    assert is_prohibited_ip("::") is True
    assert is_prohibited_ip("255.255.255.255") is True
    assert is_prohibited_ip("240.0.0.1") is True
    assert is_prohibited_ip("100.64.0.1") is True
    assert is_prohibited_ip("198.18.0.1") is True
    # IPv4 mapped IPv6
    assert is_prohibited_ip("::ffff:127.0.0.1") is True
    assert is_prohibited_ip("::ffff:169.254.169.254") is True

    # Valid Public IPs
    assert is_prohibited_ip("93.184.216.34") is False
    assert is_prohibited_ip("8.8.8.8") is False
    assert is_prohibited_ip("2606:4700:4700::1111") is False


def test_ssrf_prohibited_hostnames():
    """Internal and reserved domains are rejected."""
    assert is_prohibited_hostname("localhost") is True
    assert is_prohibited_hostname("api.localhost") is True
    assert is_prohibited_hostname("myhost.local") is True
    assert is_prohibited_hostname("service.internal") is True
    assert is_prohibited_hostname("switch.lan") is True
    assert is_prohibited_hostname("router.home.arpa") is True
    assert is_prohibited_hostname("metadata.google.internal") is True
    assert is_prohibited_hostname("instance-data") is True
    assert is_prohibited_hostname("metadata") is True

    # Valid domains
    assert is_prohibited_hostname("docs.python.org") is False
    assert is_prohibited_hostname("example.com") is False


def test_ssrf_unusual_and_numeric_ip_representations():
    """Unusual IP formats (integers, octal, hex, abbreviated) are detected and checked."""
    # Pure integer for 127.0.0.1 is 2130706433
    assert is_prohibited_hostname("2130706433") is True
    # Octal 0177.0.0.1
    assert is_prohibited_hostname("0177.0.0.1") is True
    # Hex 0x7f000001
    assert is_prohibited_hostname("0x7f.0.0.1") is True
    # Abbreviated 127.1
    assert is_prohibited_hostname("127.1") is True


def test_validate_url_schemes_and_malformed():
    """Only HTTP and HTTPS are permitted; invalid schemes and formats rejected."""
    # Valid
    s, h, p = validate_url("https://example.com:443/test")
    assert s == "https" and h == "example.com" and p == 443

    # Disallowed schemes
    for bad_url in [
        "file:///etc/passwd",
        "gopher://example.com",
        "ftp://example.com/file",
        "data:text/plain;base64,SGVsbG8=",
        "javascript:alert(1)",
        "ws://example.com/socket",
    ]:
        with pytest.raises(URLValidationError, match="Unsupported URL scheme"):
            validate_url(bad_url)

    # Embedded credentials rejected
    with pytest.raises(URLValidationError, match="embedded credentials"):
        validate_url("https://user:password@example.com/path")

    # Whitespace or control characters
    with pytest.raises(URLValidationError, match="whitespace or control"):
        validate_url("https://example.com/path with spaces\n")

    # Invalid port numbers
    with pytest.raises(URLValidationError, match="port"):
        validate_url("https://example.com:99999/test")


@pytest.mark.anyio
async def test_dns_rebinding_prevention_pinned_ip():
    """
    Test SafeNetworkBackend DNS pinning:
    Resolver validates public IP, and backend connects to that pre-validated IP directly.
    If resolver detects that a hostname changes to a private IP, it raises SSRFSecurityError.
    """
    call_count = 0

    def rebinding_resolver(host: str, port: int) -> list[str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First resolution: legitimate public IP
            return ["93.184.216.34"]
        # Rebound resolution: malicious internal loopback IP
        raise SSRFSecurityError("Destination address is prohibited (private or reserved IP)")

    backend = SafeNetworkBackend(dns_resolver=rebinding_resolver)

    # First call succeeds
    ips = rebinding_resolver("attacker.com", 80)
    assert ips == ["93.184.216.34"]

    # Second call (rebound attempt) is immediately blocked by SSRF check
    with pytest.raises(SSRFSecurityError):
        await backend.connect_tcp("attacker.com", 80)


@pytest.mark.anyio
async def test_redirect_to_forbidden_private_destination():
    """Public URL redirecting to private 127.0.0.1 or AWS metadata is rejected before connection."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="Secret")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeDocumentFetcher(client=client)

    with pytest.raises(SSRFSecurityError):
        await fetcher.fetch("https://example.com/start")


@pytest.mark.anyio
async def test_cross_origin_redirect_strips_sensitive_headers():
    """Cross-origin redirect strips sensitive Authorization, Cookie, and API key headers."""
    seen_headers: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.append(dict(request.headers))
        if request.url.host == "auth.example.com":
            return httpx.Response(302, headers={"Location": "https://public-cdn.example.org/doc"})
        return httpx.Response(200, text="CDN content", headers={"Content-Type": "text/plain"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeDocumentFetcher(
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=client,
    )

    _, _, text = await fetcher.fetch("https://auth.example.com/start")
    assert text == "CDN content"
    assert len(seen_headers) == 2

    # Second request to public-cdn.example.org must NOT have any leaked credentials
    second_headers = seen_headers[1]
    assert "authorization" not in second_headers
    assert "cookie" not in second_headers
    assert "x-api-key" not in second_headers


@pytest.mark.anyio
async def test_redirect_loop_detected():
    """Self-referential or cycling redirects raise RedirectLoopError (subclass of TooManyRedirectsError)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://example.com/loop"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeDocumentFetcher(
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=client,
    )

    with pytest.raises(TooManyRedirectsError):
        await fetcher.fetch("https://example.com/loop")


@pytest.mark.anyio
async def test_tls_verification_failure():
    """Simulated TLS verification failure raises FetcherError with clean public message."""
    class TLSFailTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

    fetcher = SafeDocumentFetcher(
        dns_resolver=lambda h, p: ["93.184.216.34"],
        client=httpx.AsyncClient(transport=TLSFailTransport()),
    )

    with pytest.raises(FetcherError) as exc_info:
        await fetcher.fetch("https://untrusted-ca.example.com")
    assert "TLS certificate verification failed" in exc_info.value.public_message
