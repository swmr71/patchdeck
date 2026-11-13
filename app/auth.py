"""Cloudflare Access の JWT を検証する。CF_ACCESS_AUD が未設定なら検証しない(ローカル試用向け)。"""
from __future__ import annotations

import logging

import jwt
from fastapi import HTTPException, Request
from jwt import PyJWKClient

from .config import settings

log = logging.getLogger("patchdeck.auth")
_jwks: PyJWKClient | None = None


def _client() -> PyJWKClient:
    global _jwks
    if _jwks is None:
        _jwks = PyJWKClient(f"https://{settings.cf_access_team_domain}/cdn-cgi/access/certs", cache_keys=True)
    return _jwks


def current_user(request: Request) -> str:
    if not settings.cf_access_aud:
        return "local"
    token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get("CF_Authorization")
    if not token:
        raise HTTPException(401, "Cloudflare Access の認証がありません")
    try:
        key = _client().get_signing_key_from_jwt(token).key
        payload = jwt.decode(token, key, algorithms=["RS256"], audience=settings.cf_access_aud,
                             issuer=f"https://{settings.cf_access_team_domain}")
    except jwt.PyJWTError as e:
        log.warning("JWT 検証失敗: %s", e)
        raise HTTPException(403, "Cloudflare Access の認証に失敗しました") from e
    return str(payload.get("email") or payload.get("sub") or "unknown")


def require_write(request: Request) -> str:
    """更新系 API: 認証+CSRF 対策(カスタムヘッダ必須。フォーム送信などのクロスサイト要求では付けられない)。"""
    user = current_user(request)
    if request.headers.get("X-PatchDeck") != "1":
        raise HTTPException(400, "X-PatchDeck ヘッダが必要です")
    return user
