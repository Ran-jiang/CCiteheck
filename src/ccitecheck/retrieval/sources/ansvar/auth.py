"""Ansvar Gateway OAuth 2.1 + PKCE 认证。

Ansvar 不发静态 API key：所有 access token 短时（分钟级）过期。本模块
实现 MCP 标准的动态客户端注册（DCR）+ 授权码 + PKCE 流程：首次浏览器
授权一次，refresh token 持久化后自动续期，过期前主动刷新。refresh
token 失效时抛 AnsvarAuthExpired，触发重新浏览器授权。

token 缓存默认 ~/.ccitecheck/ansvar_tokens.json（0600），可用
ANSVAR_TOKEN_CACHE 覆盖。无浏览器环境会打印授权 URL 供手动粘贴。
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ....infrastructure.http import BROWSER_USER_AGENT, default_ssl_context


class AnsvarAuthError(RuntimeError):
    """OAuth 认证失败。"""


class AnsvarAuthExpired(AnsvarAuthError):
    """refresh token 已失效，需重新浏览器授权。"""


_DEFAULT_REALM = "https://auth.ansvar.eu/realms/ansvar"
_DEFAULT_SCOPE = "openid profile email offline_access mcp:tools"
# 过期前留 60s 余量主动刷新，避免请求中途过期
_REFRESH_MARGIN = 60
# 本地回调服务器等待授权的最长时间（秒）
_CALLBACK_TIMEOUT = 300


@dataclass
class _OAuthEndpoints:
    authorization: str
    token: str
    registration: str


class AnsvarOAuth:
    """管理 Ansvar OAuth bearer token 的获取与刷新。"""

    def __init__(
        self,
        realm: Optional[str] = None,
        scope: str = _DEFAULT_SCOPE,
        token_cache: Optional[str | Path] = None,
        client_name: str = "ccitecheck",
    ):
        self.realm = (realm or os.getenv("ANSVAR_AUTH_REALM") or _DEFAULT_REALM).rstrip("/")
        self.scope = scope
        self.client_name = client_name
        cache = token_cache or os.getenv("ANSVAR_TOKEN_CACHE")
        self.cache_path = Path(cache) if cache else Path.home() / ".ccitecheck" / "ansvar_tokens.json"
        self._endpoints: Optional[_OAuthEndpoints] = None
        self._client_id: Optional[str] = None
        self._tokens: Optional[dict] = None
        self._client_secret = os.getenv("ANSVAR_CLIENT_SECRET") or ""
        self._service_client_id = os.getenv("ANSVAR_CLIENT_ID") or ""

    def get_bearer(self) -> str:
        """返回有效 access token；过期则用 refresh token 续，refresh 失效抛 AnsvarAuthExpired。"""
        if self._service_client_id and self._client_secret:
            if self._tokens and not _is_expired(self._tokens, margin=_REFRESH_MARGIN):
                return self._tokens["access_token"]
            self._tokens = self._client_credentials()
            return self._tokens["access_token"]
        tokens = self._load_tokens()
        if tokens and not _is_expired(tokens, margin=_REFRESH_MARGIN):
            return tokens["access_token"]
        if tokens and tokens.get("refresh_token"):
            refreshed = self._refresh(tokens["refresh_token"])
            if refreshed is not None:
                refreshed["refresh_token"] = (
                    refreshed.get("refresh_token") or tokens["refresh_token"]
                )
                self._save_tokens(refreshed)
                return refreshed["access_token"]
            # refresh 失败 → refresh token 死了，清缓存触发重新授权
            self._clear_tokens()
            raise AnsvarAuthExpired("Ansvar refresh token 已失效，需重新授权")
        # 无缓存或被清 → 触发一次完整浏览器授权
        return self._full_authorization_flow()["access_token"]

    def invalidate(self) -> None:
        """显式清除登录状态；下次 get_bearer 重新授权。"""
        self._tokens = None
        self._clear_tokens()

    def expire_access_token(self) -> None:
        """使当前 access token 失效，同时保留可用的 refresh token。"""
        if self._service_client_id and self._client_secret:
            self._tokens = None
            return
        tokens = self._load_tokens()
        if tokens:
            tokens["expires_at"] = 0
            self._save_tokens(tokens)

    # ---- 端点发现 ----

    def _discover(self) -> _OAuthEndpoints:
        if self._endpoints:
            return self._endpoints
        url = f"{self.realm}/.well-known/oauth-authorization-server"
        request = urllib.request.Request(
            url, headers={"User-Agent": BROWSER_USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=15, context=default_ssl_context()) as resp:
                meta = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError) as exc:
            raise AnsvarAuthError(f"无法发现 Ansvar OAuth 端点：{exc}") from exc
        auth = meta.get("authorization_endpoint")
        token = meta.get("token_endpoint")
        reg = meta.get("registration_endpoint")
        if not (auth and token):
            raise AnsvarAuthError(f"Ansvar OAuth 元数据缺少端点：{meta}")
        self._endpoints = _OAuthEndpoints(auth, token, reg or f"{self.realm}/clients-registrations/default")
        return self._endpoints

    # ---- DCR 动态客户端注册 ----

    def _register_client(self, redirect_uri: str) -> str:
        if self._client_id:
            return self._client_id
        endpoints = self._discover()
        body = json.dumps({
            "client_name": self.client_name,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            # 公共客户端（PKCE），无需 client_secret
            "token_endpoint_auth_method": "none",
            "scope": self.scope,
        }).encode("utf-8")
        req = urllib.request.Request(
            endpoints.registration, data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": BROWSER_USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15, context=default_ssl_context()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AnsvarAuthError(f"Ansvar DCR 注册失败 HTTP {exc.code}: {exc.read()[:200]}") from exc
        client_id = data.get("client_id")
        if not client_id:
            raise AnsvarAuthError(f"Ansvar DCR 未返回 client_id：{data}")
        self._client_id = client_id
        return client_id

    # ---- PKCE 授权码流程 ----

    def _full_authorization_flow(self) -> dict:
        redirect_port = _free_port()
        redirect_uri = f"http://127.0.0.1:{redirect_port}/callback"
        # DCR 客户端绑定 redirect_uri；重授权端口改变时必须重新注册。
        self._client_id = None
        client_id = self._register_client(redirect_uri)

        verifier = _make_code_verifier()
        challenge = _pkce_challenge(verifier)
        state = secrets.token_urlsafe(16)

        endpoints = self._discover()
        auth_url = _build_auth_url(
            endpoints.authorization, client_id, redirect_uri,
            challenge, state, self.scope,
        )

        code = _capture_auth_code(redirect_port, state, auth_url)
        if not code:
            raise AnsvarAuthError("未收到授权码（超时或被拒绝）")
        tokens = self._exchange_code(code, verifier, redirect_uri, client_id)
        self._save_tokens(tokens)
        return tokens

    def _exchange_code(self, code: str, verifier: str, redirect_uri: str, client_id: str) -> dict:
        endpoints = self._discover()
        body = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        }).encode("utf-8")
        tokens = self._post_token(body, "授权码交换")
        assert tokens is not None
        return tokens

    def _refresh(self, refresh_token: str) -> Optional[dict]:
        client_id = self._client_id or self._load_client_id()
        if not client_id:
            # 没有 client_id 就无法 refresh（公共客户端仍需 client_id）
            return None
        endpoints = self._discover()
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }).encode("utf-8")
        try:
            return self._post_token(body, "刷新", allow_failure=True)
        except AnsvarAuthError:
            return None

    def _client_credentials(self) -> dict:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": self._service_client_id,
            "client_secret": self._client_secret,
            "scope": "mcp:tools",
        }).encode("utf-8")
        tokens = self._post_token(body, "服务凭证交换")
        assert tokens is not None
        return tokens

    def _post_token(
        self, body: bytes, label: str, *, allow_failure: bool = False
    ) -> Optional[dict]:
        endpoints = self._discover()
        req = urllib.request.Request(
            endpoints.token, data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": BROWSER_USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20, context=default_ssl_context()) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if allow_failure:
                return None
            raise AnsvarAuthError(f"Ansvar token {label} 失败 HTTP {exc.code}: {exc.read()[:200]}") from exc
        if not data.get("access_token"):
            if allow_failure:
                return None
            raise AnsvarAuthError(f"Ansvar token {label} 未返回 access_token：{data}")
        return _normalize_tokens(data)

    # ---- token 持久化 ----

    def _load_tokens(self) -> Optional[dict]:
        if self._tokens is not None:
            return self._tokens
        if not self.cache_path.exists():
            return None
        try:
            data = json.loads(self.cache_path.read_text("utf-8"))
            if isinstance(data, dict) and data.get("access_token"):
                self._tokens = data
                # 恢复 client_id 以便 refresh
                if data.get("client_id"):
                    self._client_id = data["client_id"]
                return data
        except (json.JSONDecodeError, OSError):
            return None
        return None

    def _save_tokens(self, tokens: dict) -> None:
        if self._client_id:
            tokens = {**tokens, "client_id": self._client_id}
        self._tokens = tokens
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(
            self.cache_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(tokens, stream, ensure_ascii=False)
        os.chmod(self.cache_path, 0o600)

    def _clear_tokens(self) -> None:
        self._tokens = None
        try:
            if self.cache_path.exists():
                self.cache_path.unlink()
        except OSError:
            pass

    def _load_client_id(self) -> Optional[str]:
        if self._client_id:
            return self._client_id
        tokens = self._load_tokens()
        if tokens and tokens.get("client_id"):
            self._client_id = tokens["client_id"]
        return self._client_id


# ---- PKCE 辅助 ----


def _make_code_verifier() -> str:
    """RFC 7636：43-128 字符的高熵随机串。"""
    return secrets.token_urlsafe(64)[:96]


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _build_auth_url(
    authorization_endpoint: str, client_id: str, redirect_uri: str,
    challenge: str, state: str, scope: str,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": scope,
    }
    return f"{authorization_endpoint}?{urllib.parse.urlencode(params)}"


def _capture_auth_code(port: int, state: str, auth_url: str) -> Optional[str]:
    """开本地回调服务器捕获授权码，同时打开浏览器让用户授权。"""
    result: dict = {"code": None}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            code = (params.get("code") or [None])[0]
            recv_state = (params.get("state") or [None])[0]
            if code and recv_state == state:
                result["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<h1>Ansvar \xe6\x8e\x88\xe6\x9d\x83\xe6\x88\x90\xe5\x8a\x9f</h1>"
                    b"<p>\xe5\x8f\xaf\xe5\x85\xb3\xe9\x97\xad\xe6\xad\xa4\xe9\xa1\xb5\xe5\xb9\xb6\xe8\xbf\x94\xe5\x9b\x9e\xe5\x91\xbd\xe4\xbb\xa4\xe8\xa1\x8c\xe3\x80\x82</p>"
                )
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"state mismatch or missing code")
            threading.Thread(target=httpd.shutdown, daemon=True).start()

    httpd = http.server.HTTPServer(("127.0.0.1", port), _Handler)
    print(f"[Ansvar] 正在打开浏览器进行授权，请在浏览器中登录 Ansvar 并同意。")
    print(f"[Ansvar] 若浏览器未自动打开，请手动访问：\n  {auth_url}")
    try:
        webbrowser.open(auth_url)
    except webbrowser.Error:
        pass  # 无浏览器环境：已打印 URL，用户可手动访问
    timer = threading.Timer(_CALLBACK_TIMEOUT, httpd.shutdown)
    timer.daemon = True
    timer.start()
    httpd.serve_forever()
    timer.cancel()
    return result["code"]


def _free_port() -> int:
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _normalize_tokens(data: dict) -> dict:
    expires_in = data.get("expires_in")
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "token_type": data.get("token_type", "Bearer"),
        "expires_at": (time.time() + int(expires_in)) if expires_in else None,
    }


def _is_expired(tokens: dict, *, margin: int = 0) -> bool:
    expires_at = tokens.get("expires_at")
    if expires_at is None:
        return False  # 无过期信息，视为有效（由 401 兜底）
    return time.time() + margin >= expires_at


__all__ = [
    "AnsvarAuthError",
    "AnsvarAuthExpired",
    "AnsvarOAuth",
]
