from datetime import datetime

from aiogram.types import Chat, Message, User as TgUser

from app.bot.access import AccessMiddleware


def _msg(uid, text):
    return Message(message_id=1, date=datetime.now(), chat=Chat(id=uid, type="private"),
                   from_user=TgUser(id=uid, is_bot=False, first_name="x"), text=text)


async def _run(repo, msg):
    called = {}

    async def handler(event, data):
        called["user"] = data.get("user", "missing")
        return "ok"

    res = await AccessMiddleware(repo)(handler, msg, {})
    return res, called


async def test_unknown_user_dropped(repo):
    res, called = await _run(repo, _msg(99, "hi"))
    assert res is None and called == {}


async def test_blocked_user_dropped(repo):
    await repo.upsert_admin(1)
    tok = await repo.create_invite(1, 100)
    await repo.redeem_invite(tok, 2, None)
    await repo.block_user(2)
    res, called = await _run(repo, _msg(2, "/start"))
    assert called == {}


async def test_invite_start_passes_with_no_user(repo):
    res, called = await _run(repo, _msg(99, "/start inv_abc"))
    assert res == "ok" and called["user"] is None


async def test_known_user_passes(repo):
    await repo.upsert_admin(1)
    res, called = await _run(repo, _msg(1, "hi"))
    assert called["user"].is_admin
