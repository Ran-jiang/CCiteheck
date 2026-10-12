from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from ccitecheck.retrieval.sources.pkulaw.client import (
    PkulawMcpClient,
    PkulawMcpError,
    PkulawNotConfiguredError,
)


def test_ssl_error_is_retried(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING]", request=request)
        return httpx.Response(200, json={"result": {}})

    client_http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.pkulaw_http_client",
        lambda: client_http,
    )
    monkeypatch.setattr("ccitecheck.retrieval.sources.pkulaw.client.time.sleep", lambda _: None)
    client = PkulawMcpClient(access_token="test")
    assert client._call_tool("/test", "test", {}) == {"result": {}}
    assert calls == 2


def test_missing_token_fails_before_network(monkeypatch):
    monkeypatch.setenv("PKULAW_ACCESS_TOKEN", "")
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.pkulaw_http_client",
        lambda *args, **kwargs: pytest.fail("network should not be called"),
    )

    with pytest.raises(PkulawNotConfiguredError, match="PKULAW_ACCESS_TOKEN"):
        PkulawMcpClient()._call_tool("/test", "test", {})


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "鉴权失败"),
        (403, "鉴权失败"),
        (429, "请求额度受限"),
    ],
)
def test_account_failures_open_circuit_without_repeated_calls(
    monkeypatch, status, message
):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, json={"error": "detail"})

    client_http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.pkulaw_http_client",
        lambda: client_http,
    )
    client = PkulawMcpClient(access_token="test")

    with pytest.raises(PkulawMcpError, match=message):
        client._call_tool("/test", "test", {})
    with pytest.raises(PkulawMcpError, match="暂停重复请求"):
        client._call_tool("/test", "test", {})

    assert calls == 1


def test_concurrent_auth_failures_only_probe_upstream_once(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="unauthorized")

    client_http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.pkulaw_http_client",
        lambda: client_http,
    )
    client = PkulawMcpClient(access_token="test")

    def invoke():
        with pytest.raises(PkulawMcpError, match="鉴权失败"):
            client._call_tool("/test", "test", {})

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: invoke(), range(24)))

    assert calls == 1


def test_exhausted_service_failures_open_short_circuit(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(503, text="unavailable")

    client_http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.pkulaw_http_client",
        lambda: client_http,
    )
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.time.sleep", lambda _: None
    )
    client = PkulawMcpClient(access_token="test")

    with pytest.raises(PkulawMcpError, match="服务暂时不可用"):
        client._call_tool("/test", "test", {})
    with pytest.raises(PkulawMcpError, match="暂停重复请求"):
        client._call_tool("/test", "test", {})

    assert calls == 3


def test_exhausted_network_failure_does_not_block_later_requests(monkeypatch):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("TLS handshake timed out", request=request)

    client_http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.pkulaw_http_client",
        lambda: client_http,
    )
    monkeypatch.setattr(
        "ccitecheck.retrieval.sources.pkulaw.client.time.sleep", lambda _: None
    )
    client = PkulawMcpClient(access_token="test")

    with pytest.raises(PkulawMcpError, match="handshake"):
        client._call_tool("/test", "test", {})
    with pytest.raises(PkulawMcpError, match="handshake") as second:
        client._call_tool("/test", "test", {})

    assert "暂停重复请求" not in str(second.value)
    assert calls == 6
