import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest

from app import oauth


def test_pkce_s256():
    v, c = oauth.make_pkce()
    assert 43 <= len(v) <= 128
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_authorize_url():
    q = parse_qs(urlparse(oauth.build_authorize_url("CH", "ST")).query)
    assert q["client_id"] == [oauth.CLIENT_ID]
    assert q["redirect_uri"] == [oauth.REDIRECT_URI]
    assert q["code_challenge"] == ["CH"] and q["state"] == ["ST"]
    assert q["code_challenge_method"] == ["S256"]


@pytest.mark.parametrize("text,expected", [
    ("abc#st", ("abc", "st")),
    ("  abc  ", ("abc", None)),
    ("https://platform.claude.com/oauth/code/callback?code=abc&state=st", ("abc", "st")),
    ("https://platform.claude.com/oauth/code/callback?code=abc#st", ("abc", "st")),
])
def test_parse_code_input(text, expected):
    assert oauth.parse_code_input(text) == expected


def test_parse_code_empty():
    with pytest.raises(ValueError):
        oauth.parse_code_input("   ")


async def test_exchange_code(fake_server, monkeypatch):
    monkeypatch.setattr(oauth, "TOKEN_URLS", [fake_server.url("/t1"), fake_server.url("/t2")])
    fake_server.add("POST", "/t1", json={
        "access_token": "A", "refresh_token": "R", "expires_in": 3600,
        "account": {"email_address": "u@x.com"},
    })
    async with aiohttp.ClientSession() as http:
        ts = await oauth.exchange_code(http, "c", "s", "v")
    body = fake_server.requests[0][2]
    assert ts.access_token == "A" and ts.refresh_token == "R" and ts.email == "u@x.com"
    assert ts.expires_at is not None
    assert body["grant_type"] == "authorization_code" and body["code_verifier"] == "v"


async def test_refresh_falls_back_and_keeps_refresh(fake_server, monkeypatch):
    monkeypatch.setattr(oauth, "TOKEN_URLS", [fake_server.url("/t1"), fake_server.url("/t2")])
    fake_server.add("POST", "/t1", status=404, text="not found")
    fake_server.add("POST", "/t2", json={"access_token": "A2"})
    async with aiohttp.ClientSession() as http:
        ts = await oauth.refresh_tokens(http, "R1")
    assert ts.access_token == "A2" and ts.refresh_token == "R1"


async def test_invalid_grant(fake_server, monkeypatch):
    monkeypatch.setattr(oauth, "TOKEN_URLS", [fake_server.url("/t1"), fake_server.url("/t2")])
    fake_server.add("POST", "/t1", status=400, json={"error": "invalid_grant"})
    async with aiohttp.ClientSession() as http:
        with pytest.raises(oauth.InvalidGrant):
            await oauth.refresh_tokens(http, "dead")
