import pytest
import httpx

from forge.research.fetcher import (
    ContentTooLargeError,
    FetcherError,
    SafeDocumentFetcher,
    SSRFSecurityError,
    TooManyRedirectsError,
    UnsupportedContentTypeError,
    URLValidationError,
    is_content_type_allowed,
    is_prohibited_hostname,
    is_prohibited_ip,
    safe_resolve_hostname,
    validate_url,
)


def test_is_prohibited_ip_ipv4():
    # Loopback
    assert is_prohibited_ip("127.0.0.1") is True
    assert is_prohibited_ip("127.255.255.255") is True
    # Private
    assert is_prohibited_ip("10.0.0.1") is True
    assert is_prohibited_ip("192.168.1.1") is True
    assert is_prohibited_ip("172.16.0.1") is True
    assert is_prohibited_ip("172.31.255.255") is True
    # Link local / metadata
    assert is_prohibited_ip("169.254.169.254") is True
    # Broadcast / Unspecified / Reserved
    assert is_prohibited_ip("0.0.0.0") is True
    assert is_prohibited_ip("255.255.255.255") is True
    assert is_prohibited_ip("100.64.0.1") is True
    assert is_prohibited_ip("192.0.2.1") is True
    assert is_prohibited_ip("198.51.100.1") is True
    assert is_prohibited_ip("203.0.113.1") is True
    assert is_prohibited_ip("198.18.0.1") is True
    assert is_prohibited_ip("224.0.0.1") is True

    # Public valid IP
    assert is_prohibited_ip("93.184.216.34") is False
    assert is_prohibited_ip("8.8.8.8") is False


def test_is_prohibited_ip_ipv6():
    # Loopback
    assert is_prohibited_ip("::1") is True
    assert is_prohibited_ip("::") is True
    # Link local
    assert is_prohibited_ip("fe80::1") is True
    # Unique local / private
    assert is_prohibited_ip("fc00::1") is True
    assert is_prohibited_ip("fd12:3456:789a::1") is True
    # IPv4 mapped loopback
    assert is_prohibited_ip("::ffff:127.0.0.1") is True
    assert is_prohibited_ip("::ffff:192.168.1.1") is True
    # NAT64 and documentation
    assert is_prohibited_ip("64:ff9b::1") is True
    assert is_prohibited_ip("2001:db8::1") is True

    # Public valid IPv6
    assert is_prohibited_ip("2606:4700:4700::1111") is False


def test_is_prohibited_hostname():
    assert is_prohibited_hostname("localhost") is True
    assert is_prohibited_hostname("localhost.localdomain") is True
    assert is_prohibited_hostname("dev.localhost") is True
    assert is_prohibited_hostname("my-service.local") is True
    assert is_prohibited_hostname("backend.internal") is True
    assert is_prohibited_hostname("router.lan") is True
    assert is_prohibited_hostname("example.com") is False
    assert is_prohibited_hostname("docs.pydantic.dev") is False


def test_validate_url():
    # Valid
    scheme, host, port = validate_url("https://example.com/docs")
    assert scheme == "https"
    assert host == "example.com"
    assert port == 443

    scheme, host, port = validate_url("http://example.com:8080/api")
    assert scheme == "http"
    assert host == "example.com"
    assert port == 8080

    # Disallowed schemes
    with pytest.raises(URLValidationError, match="Unsupported URL scheme"):
        validate_url("file:///etc/passwd")

    with pytest.raises(URLValidationError, match="Unsupported URL scheme"):
        validate_url("ftp://example.com/file.txt")

    with pytest.raises(URLValidationError, match="Unsupported URL scheme"):
        validate_url("javascript:alert(1)")

    # Credentials
    with pytest.raises(URLValidationError, match="embedded credentials"):
        validate_url("https://user:pass@example.com/docs")


def test_safe_resolve_hostname_rejects_ssrf():
    with pytest.raises(SSRFSecurityError):
        safe_resolve_hostname("localhost")

    with pytest.raises(SSRFSecurityError):
        safe_resolve_hostname("127.0.0.1")

    with pytest.raises(SSRFSecurityError):
        safe_resolve_hostname("169.254.169.254")

    with pytest.raises(SSRFSecurityError):
        safe_resolve_hostname("internal.local")


def test_is_content_type_allowed():
    assert is_content_type_allowed("text/html; charset=utf-8") is True
    assert is_content_type_allowed("text/plain") is True
    assert is_content_type_allowed("text/markdown") is True
    assert is_content_type_allowed("application/json") is True
    assert is_content_type_allowed("application/octet-stream") is False
    assert is_content_type_allowed("image/png") is False
    assert is_content_type_allowed("application/zip") is False


@pytest.mark.anyio
async def test_fetcher_success_mock_client():
    mock_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text="<html><head><title>Test Doc</title></head><body><h1>Hello</h1></body></html>",
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
    )
    client = httpx.AsyncClient(transport=mock_transport)
    fetcher = SafeDocumentFetcher(
        dns_resolver=lambda host, port: ["93.184.216.34"],
        client=client,
    )

    final_url, content_type, text = await fetcher.fetch("https://example.com/docs")
    assert final_url == "https://example.com/docs"
    assert "text/html" in content_type
    assert "Hello" in text


@pytest.mark.anyio
async def test_fetcher_rejects_redirect_to_private_ip():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/docs":
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/admin"})
        return httpx.Response(200, text="Admin secret")

    mock_transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=mock_transport)

    def resolver(host, port):
        if host in {"127.0.0.1", "localhost"}:
            raise SSRFSecurityError("Destination address is prohibited")
        return ["93.184.216.34"]

    fetcher = SafeDocumentFetcher(dns_resolver=resolver, client=client)

    with pytest.raises(SSRFSecurityError):
        await fetcher.fetch("https://example.com/docs")


@pytest.mark.anyio
async def test_fetcher_excessive_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://example.com/redirect-loop"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeDocumentFetcher(
        max_redirects=3,
        dns_resolver=lambda host, port: ["93.184.216.34"],
        client=client,
    )

    with pytest.raises(TooManyRedirectsError):
        await fetcher.fetch("https://example.com/start")


@pytest.mark.anyio
async def test_fetcher_content_size_limit():
    large_text = "x" * 5000
    mock_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text=large_text,
            headers={"Content-Type": "text/plain", "Content-Length": "5000"},
        )
    )
    client = httpx.AsyncClient(transport=mock_transport)
    fetcher = SafeDocumentFetcher(
        max_response_bytes=1000,
        dns_resolver=lambda host, port: ["93.184.216.34"],
        client=client,
    )

    with pytest.raises(ContentTooLargeError):
        await fetcher.fetch("https://example.com/large")


@pytest.mark.anyio
async def test_fetcher_rejects_unsupported_content_type():
    mock_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            content=b"\x89PNG\r\n\x1a\n",
            headers={"Content-Type": "image/png"},
        )
    )
    client = httpx.AsyncClient(transport=mock_transport)
    fetcher = SafeDocumentFetcher(
        dns_resolver=lambda host, port: ["93.184.216.34"],
        client=client,
    )

    with pytest.raises(UnsupportedContentTypeError):
        await fetcher.fetch("https://example.com/image.png")
