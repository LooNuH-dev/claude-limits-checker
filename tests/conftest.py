import json as _json
from collections import defaultdict

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.crypto import Cipher, generate_key
from app.db import Repo


@pytest.fixture
def cipher():
    return Cipher(generate_key())


@pytest.fixture
async def repo(tmp_path, cipher):
    r = await Repo.open(str(tmp_path / "t.db"), cipher)
    yield r
    await r.close()


class FakeServer:
    def __init__(self, server: TestServer):
        self._server = server
        self._queues: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.requests: list[tuple[str, str, dict | None, dict]] = []

    def add(self, method: str, path: str, status: int = 200,
            json: dict | None = None, text: str | None = None,
            headers: dict | None = None) -> None:
        self._queues[(method.upper(), path)].append({
            "status": status, "json": json, "text": text, "headers": headers or {},
        })

    def url(self, path: str) -> str:
        return f"http://{self._server.host}:{self._server.port}{path}"

    async def _handle(self, request: web.Request) -> web.Response:
        raw = await request.read()
        try:
            body = _json.loads(raw) if raw else None
        except ValueError:
            body = None
        self.requests.append((request.method, request.path, body, dict(request.headers)))
        queue = self._queues[(request.method, request.path)]
        if not queue:
            return web.Response(status=599, text="no more responses")
        resp = queue.pop(0)
        if resp["json"] is not None:
            return web.json_response(resp["json"], status=resp["status"], headers=resp["headers"])
        return web.Response(text=resp["text"] or "", status=resp["status"], headers=resp["headers"])


@pytest.fixture
async def fake_server():
    app = web.Application()
    fs_holder = {}

    async def handler(request):
        return await fs_holder["fs"]._handle(request)

    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    fs = FakeServer(server)
    fs_holder["fs"] = fs
    yield fs
    await server.close()
