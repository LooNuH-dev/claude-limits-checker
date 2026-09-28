async def test_admin_upsert(repo):
    await repo.upsert_admin(1)
    u = await repo.get_user(1)
    assert u.is_admin and not u.blocked


async def test_invite_one_time(repo):
    await repo.upsert_admin(1)
    tok = await repo.create_invite(1, ttl=100, now=1000)
    assert await repo.redeem_invite(tok, 2, "bob", now=1050)
    assert not await repo.redeem_invite(tok, 3, "eve", now=1051)
    assert (await repo.get_user(2)).role == "user"
    assert await repo.get_user(3) is None


async def test_invite_expired(repo):
    tok = await repo.create_invite(1, ttl=100, now=1000)
    assert not await repo.redeem_invite(tok, 2, None, now=1100)
    assert not await repo.redeem_invite("nope", 2, None, now=1000)


async def test_accounts_isolated_and_encrypted(repo, tmp_path):
    await repo.upsert_admin(1)
    tok = await repo.create_invite(1, 100, now=0)
    await repo.redeem_invite(tok, 2, None, now=1)
    a = await repo.add_account(1, "mine", "a@x", "acc-SECRET", "ref-SECRET", 500)
    assert await repo.get_account(a, owner=2) is None
    assert (await repo.get_account(a, owner=1)).refresh_token == "ref-SECRET"
    assert await repo.list_accounts(2) == []
    raw = (tmp_path / "t.db").read_bytes()
    assert b"SECRET" not in raw


async def test_account_ops(repo):
    await repo.upsert_admin(1)
    a = await repo.add_account(1, "x", None, "a", "r", None)
    assert await repo.rename_account(a, 1, "new")
    assert await repo.toggle_notify(a, 1) is False
    assert await repo.toggle_notify(a, 1) is True
    await repo.set_needs_relogin(a)
    assert await repo.list_active_accounts() == []
    await repo.update_tokens(a, "a2", "r2", 10)
    acc = (await repo.list_active_accounts())[0]
    assert acc.label == "new" and acc.access_token == "a2" and not acc.needs_relogin
    assert not await repo.delete_account(a, 2)
    assert await repo.delete_account(a, 1)
    assert await repo.get_account(a) is None


async def test_block_user_removes_accounts(repo):
    await repo.upsert_admin(1)
    tok = await repo.create_invite(1, 100, now=0)
    await repo.redeem_invite(tok, 2, None, now=1)
    a = await repo.add_account(2, "x", None, "a", "r", None)
    await repo.save_check(a, 50.0, False, None)
    assert await repo.block_user(2)
    assert (await repo.get_user(2)).blocked
    assert await repo.get_account(a) is None
    assert await repo.get_state(a) is None
    assert not await repo.block_user(1)  # админа не блокируем


async def test_state(repo):
    await repo.upsert_admin(1)
    a = await repo.add_account(1, "x", None, "a", "r", None)
    assert await repo.get_was_limited(a) is False
    await repo.save_check(a, 100.0, True, None, now=5)
    assert await repo.get_was_limited(a) is True
    await repo.save_check(a, None, True, "boom", now=6)
    st = await repo.get_state(a)
    assert st["last_error"] == "boom" and st["last_5h_percent"] == 100.0


async def test_pending(repo):
    await repo.save_pending("s1", 7, "verifier", None, now=100)
    assert await repo.pop_pending("s1", 8, now=101) is None       # чужой tg_id
    assert await repo.pop_pending("s1", 7, now=101) == ("verifier", None)
    assert await repo.pop_pending("s1", 7, now=102) is None       # одноразово
    await repo.save_pending("s2", 7, "v", 3, now=100)
    assert await repo.pop_pending("s2", 7, now=701) is None       # TTL 600


async def test_migration_from_v1(tmp_path):
    import aiosqlite
    from app.crypto import Cipher, generate_key
    from app.db import SCHEMA, Repo
    path = str(tmp_path / "v1.db")
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA.replace("    was_weekly_limited INTEGER NOT NULL DEFAULT 0,\n", ""))
        await db.execute("PRAGMA user_version=1")
        await db.commit()
    repo = await Repo.open(path, Cipher(generate_key()))
    try:
        assert await repo.get_was_weekly_limited(1) is False
        async with repo.db.execute("PRAGMA table_info(account_state)") as cur:
            assert "was_weekly_limited" in [r["name"] for r in await cur.fetchall()]
    finally:
        await repo.close()
