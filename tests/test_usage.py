import aiohttp
import pytest

from app import oauth, usage

USAGE = {"five_hour": {"utilization": 42.0, "resets_at": None}}


async def _acc(repo, expires_at=None):
    await repo.upsert_admin(1)
    aid = await repo.add_account(1, "x", None, "OLD", "R", expires_at)
    return await repo.get_account(aid)


async def test_fetch_ok(repo, fake_server, monkeypatch):
    monkeypatch.setattr(usage, "USAGE_URL", fake_server.url("/usage"))
    acc = await _acc(repo)
    fake_server.add("GET", "/usage", json=USAGE)
    async with aiohttp.ClientSession() as http:
        assert await usage.fetch_account_usage(http, repo, acc) == USAGE


async def test_401_refreshes_and_saves(repo, fake_server, monkeypatch):
    monkeypatch.setattr(usage, "USAGE_URL", fake_server.url("/usage"))
    monkeypatch.setattr(oauth, "TOKEN_URLS", [fake_server.url("/t1"), fake_server.url("/t2")])
    acc = await _acc(repo)
    fake_server.add("GET", "/usage", status=401)
    fake_server.add("POST", "/t1", json={"access_token": "NEW", "refresh_token": "R2"})
    fake_server.add("GET", "/usage", json=USAGE)
    async with aiohttp.ClientSession() as http:
        assert await usage.fetch_account_usage(http, repo, acc) == USAGE
    saved = await repo.get_account(acc.id)
    assert saved.access_token == "NEW" and saved.refresh_token == "R2"


async def test_expired_refreshes_first(repo, fake_server, monkeypatch):
    monkeypatch.setattr(usage, "USAGE_URL", fake_server.url("/usage"))
    monkeypatch.setattr(oauth, "TOKEN_URLS", [fake_server.url("/t1"), fake_server.url("/t2")])
    acc = await _acc(repo, expires_at=1)
    fake_server.add("POST", "/t1", json={"access_token": "NEW"})
    fake_server.add("GET", "/usage", json=USAGE)
    async with aiohttp.ClientSession() as http:
        await usage.fetch_account_usage(http, repo, acc)
    assert (await repo.get_account(acc.id)).access_token == "NEW"


async def test_invalid_grant_marks_relogin(repo, fake_server, monkeypatch):
    monkeypatch.setattr(usage, "USAGE_URL", fake_server.url("/usage"))
    monkeypatch.setattr(oauth, "TOKEN_URLS", [fake_server.url("/t1"), fake_server.url("/t2")])
    acc = await _acc(repo)
    fake_server.add("GET", "/usage", status=401)
    fake_server.add("POST", "/t1", status=400, json={"error": "invalid_grant"})
    async with aiohttp.ClientSession() as http:
        with pytest.raises(usage.NeedsRelogin):
            await usage.fetch_account_usage(http, repo, acc)
    assert (await repo.get_account(acc.id)).needs_relogin


async def test_429_retry(fake_server, monkeypatch):
    monkeypatch.setattr(usage, "USAGE_URL", fake_server.url("/usage"))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(usage.asyncio, "sleep", no_sleep)
    fake_server.add("GET", "/usage", status=429, headers={"retry-after": "1"})
    fake_server.add("GET", "/usage", json=USAGE)
    async with aiohttp.ClientSession() as http:
        assert await usage.fetch_usage(http, "T") == USAGE
