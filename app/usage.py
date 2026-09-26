import asyncio
import time

import aiohttp

from app import oauth
from app.db import Account, Repo

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TIMEOUT = aiohttp.ClientTimeout(total=10)
REFRESH_SKEW = 60


class NeedsRelogin(Exception):
    pass


class _Unauthorized(Exception):
    pass


async def fetch_usage(http: aiohttp.ClientSession, access_token: str, retries: int = 2) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-version": "2023-06-01",
        "User-Agent": oauth.USER_AGENT,
        "Content-Type": "application/json",
    }
    for attempt in range(retries + 1):
        async with http.get(USAGE_URL, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status == 401:
                raise _Unauthorized()
            if resp.status in (429, 500, 502, 503, 504) and attempt < retries:
                try:
                    wait = float(resp.headers.get("retry-after", ""))
                except ValueError:
                    wait = 2.0 * (attempt + 1)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError("unreachable")


async def _refresh(http: aiohttp.ClientSession, repo: Repo, account: Account) -> str:
    try:
        ts = await oauth.refresh_tokens(http, account.refresh_token)
    except oauth.InvalidGrant as e:
        await repo.set_needs_relogin(account.id)
        raise NeedsRelogin(str(e)) from e
    await repo.update_tokens(account.id, ts.access_token, ts.refresh_token, ts.expires_at)
    account.access_token, account.refresh_token = ts.access_token, ts.refresh_token
    account.expires_at = ts.expires_at
    return ts.access_token


async def fetch_account_usage(http: aiohttp.ClientSession, repo: Repo, account: Account) -> dict:
    token = account.access_token
    if account.expires_at and account.expires_at - REFRESH_SKEW <= time.time():
        token = await _refresh(http, repo, account)
    try:
        return await fetch_usage(http, token)
    except _Unauthorized:
        token = await _refresh(http, repo, account)
        try:
            return await fetch_usage(http, token)
        except _Unauthorized as e:
            await repo.set_needs_relogin(account.id)
            raise NeedsRelogin("Токен не принят после обновления") from e
