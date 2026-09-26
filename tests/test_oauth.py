import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from aioresponses import aioresponses

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


async def test_exchange_code():
    with aioresponses() as m:
        m.post(oauth.TOKEN_URLS[0], body='{"access_token": "A", "refresh_token": "R", "expires_in": 3600, "account": {"email_address": "u@x.com"}}')
        async with aiohttp.ClientSession() as http:
            ts = await oauth.exchange_code(http, "c", "s", "v")
        body = list(m.requests.values())[0][0].kwargs["json"]
    assert ts.access_token == "A" and ts.refresh_token == "R" and ts.email == "u@x.com"
    assert ts.expires_at is not None
    assert body["grant_type"] == "authorization_code" and body["code_verifier"] == "v"


async def test_refresh_falls_back_and_keeps_refresh():
    with aioresponses() as m:
        m.post(oauth.TOKEN_URLS[0], status=404, body="not found")
        m.post(oauth.TOKEN_URLS[1], body='{"access_token": "A2"}')
        async with aiohttp.ClientSession() as http:
            ts = await oauth.refresh_tokens(http, "R1")
    assert ts.access_token == "A2" and ts.refresh_token == "R1"


async def test_invalid_grant():
    with aioresponses() as m:
        m.post(oauth.TOKEN_URLS[0], status=400, body='{"error": "invalid_grant"}')
        async with aiohttp.ClientSession() as http:
            with pytest.raises(oauth.InvalidGrant):
                await oauth.refresh_tokens(http, "dead")
