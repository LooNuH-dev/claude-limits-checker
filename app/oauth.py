import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = "user:profile user:inference"
TOKEN_URLS = [
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
]
USER_AGENT = "claude-code/0.2.29"
TIMEOUT = aiohttp.ClientTimeout(total=15)


class OAuthError(Exception):
    pass


class InvalidGrant(OAuthError):
    pass


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: int | None
    email: str | None


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(32)


def build_authorize_url(challenge: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })


def parse_code_input(text: str) -> tuple[str, str | None]:
    """Принимает `code#state`, голый код или URL callback-страницы целиком."""
    text = text.strip()
    if text.startswith("http"):
        parsed = urlparse(text)
        q = parse_qs(parsed.query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [None])[0] or (parsed.fragment or None)
    elif "#" in text:
        code, state = text.split("#", 1)
        state = state or None
    else:
        code, state = text, None
    if not code:
        raise ValueError("пустой код")
    return code, state


async def _post_token(http: aiohttp.ClientSession, payload: dict,
                      fallback_refresh: str | None = None) -> TokenSet:
    last_err: Exception | None = None
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": USER_AGENT}
    for url in TOKEN_URLS:
        try:
            async with http.post(url, json=payload, headers=headers, timeout=TIMEOUT) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    if "invalid_grant" in raw.lower():
                        raise InvalidGrant("Сессия истекла или код недействителен (invalid_grant)")
                    last_err = OAuthError(f"HTTP {resp.status} на {url}: {raw[:300] or 'без деталей'}")
                    # 404/405/400 без invalid_grant — возможно, эндпоинт переехал
                    if resp.status in (400, 404, 405):
                        continue
                    raise last_err
                data = json.loads(raw)
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as e:
            last_err = OAuthError(f"Сеть/ответ {url}: {e}")
            continue
        access = data.get("access_token")
        if not access:
            raise OAuthError("Ответ OAuth не содержит access_token")
        expires_in = data.get("expires_in")
        return TokenSet(
            access_token=access,
            refresh_token=data.get("refresh_token") or fallback_refresh or "",
            expires_at=int(time.time()) + int(expires_in) if expires_in else None,
            email=(data.get("account") or {}).get("email_address"),
        )
    raise last_err or OAuthError("Не удалось получить токен")


async def exchange_code(http: aiohttp.ClientSession, code: str, state: str,
                        verifier: str) -> TokenSet:
    return await _post_token(http, {
        "grant_type": "authorization_code",
        "code": code,
        "state": state,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })


async def refresh_tokens(http: aiohttp.ClientSession, refresh_token: str) -> TokenSet:
    return await _post_token(http, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }, fallback_refresh=refresh_token)
