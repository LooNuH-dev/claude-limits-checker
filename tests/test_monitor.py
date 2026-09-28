import pytest

from app import monitor, usage


@pytest.mark.parametrize("was,p,exp", [
    (False, 99.9, None), (False, 100.0, "limited"), (True, 100.0, None), (True, 50.0, "reset"),
])
def test_transition(was, p, exp):
    assert monitor.transition(was, p) == exp


async def _setup(repo, notify=True):
    await repo.upsert_admin(1)
    aid = await repo.add_account(1, "acc", None, "A", "R", None)
    if not notify:
        await repo.toggle_notify(aid, 1)
    return await repo.get_account(aid)


def _fake_usage(monkeypatch, result):
    async def fake(http, repo, account):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(monitor.usage, "fetch_account_usage", fake)


async def test_limit_then_reset(repo, monkeypatch):
    acc = await _setup(repo)
    _fake_usage(monkeypatch, {"five_hour": {"utilization": 100}})
    n = await monitor.check_account(None, repo, acc)
    assert n.chat_id == 1 and "100%" in n.text
    assert await monitor.check_account(None, repo, acc) is None  # без дублей
    _fake_usage(monkeypatch, {"five_hour": {"utilization": 3}})
    n = await monitor.check_account(None, repo, acc)
    assert "сбросились" in n.text


async def test_notify_disabled_still_tracks_state(repo, monkeypatch):
    acc = await _setup(repo, notify=False)
    _fake_usage(monkeypatch, {"five_hour": {"utilization": 100}})
    assert await monitor.check_account(None, repo, acc) is None
    assert await repo.get_was_limited(acc.id)


async def test_error_saved_without_notice(repo, monkeypatch):
    acc = await _setup(repo)
    _fake_usage(monkeypatch, RuntimeError("HTTP 503"))
    assert await monitor.check_account(None, repo, acc) is None
    assert (await repo.get_state(acc.id))["last_error"] == "HTTP 503"


async def test_relogin_notice(repo, monkeypatch):
    acc = await _setup(repo)
    _fake_usage(monkeypatch, usage.NeedsRelogin("x"))
    n = await monitor.check_account(None, repo, acc)
    assert n.relogin_account_id == acc.id


async def test_weekly_limit_then_reset(repo, monkeypatch):
    acc = await _setup(repo)
    _fake_usage(monkeypatch, {"five_hour": {"utilization": 10}, "seven_day": {"utilization": 100}})
    n = await monitor.check_account(None, repo, acc)
    assert "недельный лимит" in n.text
    assert await repo.get_was_weekly_limited(acc.id)
    assert await monitor.check_account(None, repo, acc) is None  # без дублей
    _fake_usage(monkeypatch, {"five_hour": {"utilization": 10}, "seven_day": {"utilization": 0}})
    n = await monitor.check_account(None, repo, acc)
    assert "Недельный лимит" in n.text and "сбросился" in n.text


async def test_weekly_state_kept_on_error(repo, monkeypatch):
    acc = await _setup(repo)
    _fake_usage(monkeypatch, {"seven_day": {"utilization": 100}})
    await monitor.check_account(None, repo, acc)
    _fake_usage(monkeypatch, RuntimeError("HTTP 503"))
    await monitor.check_account(None, repo, acc)
    assert await repo.get_was_weekly_limited(acc.id)
