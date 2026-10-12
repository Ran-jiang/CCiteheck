"""共享 HTTP/TLS 基础设施。"""

from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import json
import random
import ssl
import threading
import time
from typing import Any, Callable

import httpx


_SSL_CONTEXT: ssl.SSLContext | None = None
_HTTP_CLIENT: httpx.Client | None = None
_HTTP_CLIENT_LOCK = threading.Lock()

# Ansvar（auth.ansvar.eu / gateway.ansvar.eu）位于 Cloudflare 之后，按 UA 签名
# 拦截 Python-urllib 默认头（HTTP 403, error code 1010）；DCR/token/MCP 端点都需要
# 标准浏览器 UA 才放行。
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def default_ssl_context() -> ssl.SSLContext:
    """返回进程内复用的默认 TLS context。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        try:
            import certifi

            _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def shared_http_client() -> httpx.Client:
    """返回线程安全的进程级连接池。"""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        with _HTTP_CLIENT_LOCK:
            if _HTTP_CLIENT is None:
                # 本机环境变量代理会掐断 DashScope TLS，因此绕过代理直连。
                # TUN 模式代理工作在应用层之外，无法通过 trust_env=False 绕过。
                _HTTP_CLIENT = httpx.Client(
                    verify=default_ssl_context(),
                    trust_env=False,
                    limits=httpx.Limits(
                        max_connections=8,
                        max_keepalive_connections=4,
                        keepalive_expiry=30.0,
                    ),
                    timeout=httpx.Timeout(
                        connect=10.0,
                        read=60.0,
                        write=10.0,
                        pool=10.0,
                    ),
                )
    return _HTTP_CLIENT


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    budget_seconds: float = 90.0
    read_timeout: float = 60.0
    backoff_base: float = 1.0
    backoff_cap: float = 15.0
    minimum_attempt_seconds: float = 5.0


class HttpRequestError(RuntimeError):
    def __init__(self, message: str, error_code: str, attempts: list[str]):
        self.error_code = error_code
        self.attempts = tuple(attempts)
        summary = _attempt_summary(attempts)
        super().__init__(f"{message} ({summary})" if summary else message)


class HttpResponseJSONError(ValueError):
    pass


def post_json_with_retry(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """在单次调用预算内 POST JSON，并按错误分类重试。"""
    deadline = clock() + policy.budget_seconds
    attempts: list[str] = []
    client = shared_http_client()

    for attempt in range(policy.max_attempts):
        remaining = deadline - clock()
        if remaining < policy.minimum_attempt_seconds:
            break
        timeout = httpx.Timeout(
            connect=min(10.0, remaining),
            read=min(policy.read_timeout, remaining),
            write=min(10.0, remaining),
            pool=min(10.0, remaining),
        )
        retry_after: float | None = None
        try:
            response = client.post(url, json=payload, headers=headers, timeout=timeout)
            if response.status_code == 429:
                error_code = "rate_limited"
                retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
            elif response.status_code >= 500:
                error_code = "upstream_error"
            elif response.status_code >= 400:
                attempts.append("http_4xx")
                raise HttpRequestError(
                    f"HTTP {response.status_code}: {response.text[:300]}",
                    "http_4xx",
                    attempts,
                )
            else:
                try:
                    data = response.json()
                except (json.JSONDecodeError, ValueError) as exc:
                    raise HttpResponseJSONError(
                        f"HTTP response was not valid JSON: {exc}"
                    ) from exc
                if not isinstance(data, dict):
                    raise HttpResponseJSONError("HTTP JSON response must be an object")
                return data
        except httpx.TimeoutException:
            error_code = "timeout"
        except httpx.TransportError:
            error_code = "transport_error"

        attempts.append(error_code)
        remaining = deadline - clock()
        if attempt + 1 >= policy.max_attempts:
            break
        if retry_after is not None:
            if retry_after > remaining:
                break
            delay = retry_after
        else:
            ceiling = min(
                policy.backoff_cap,
                policy.backoff_base * (2 ** attempt),
            )
            delay = random.uniform(0.0, ceiling)
        delay = min(delay, max(0.0, remaining))
        if delay:
            sleep(delay)

    final_code = attempts[-1] if attempts else "timeout"
    raise HttpRequestError("HTTP request failed", final_code, attempts)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _attempt_summary(attempts: list[str]) -> str:
    if not attempts:
        return "0 attempts"
    ordered: list[str] = []
    for code in attempts:
        if code not in ordered:
            ordered.append(code)
    details = ", ".join(f"{code}×{attempts.count(code)}" for code in ordered)
    return f"{len(attempts)} attempts: {details}"


__all__ = [
    "BROWSER_USER_AGENT",
    "HttpRequestError",
    "HttpResponseJSONError",
    "RetryPolicy",
    "default_ssl_context",
    "post_json_with_retry",
    "shared_http_client",
]
