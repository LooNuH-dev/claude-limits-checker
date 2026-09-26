import asyncio
import logging
from dataclasses import dataclass

from app import report, usage
from app.db import Account, Repo

log = logging.getLogger(__name__)
CONCURRENCY = 5


@dataclass
class Notice:
    chat_id: int
    text: str
    relogin_account_id: int | None = None


def transition(was_limited: bool, percent: float) -> str | None:
    is_limited = percent >= 100.0
    if is_limited and not was_limited:
        return "limited"
    if was_limited and not is_limited:
        return "reset"
    return None


async def check_account(http, repo: Repo, account: Account) -> Notice | None:
    was_limited = await repo.get_was_limited(account.id)
    try:
        data = await usage.fetch_account_usage(http, repo, account)
    except usage.NeedsRelogin as e:
        await repo.save_check(account.id, None, was_limited, f"нужен повторный вход: {e}")
        return Notice(account.owner_tg_id, report.relogin_text(account.label), account.id)
    except Exception as e:  # сеть, 5xx, 429 после ретраев — молча в last_error
        log.warning("[%s] %s", account.id, e)
        await repo.save_check(account.id, None, was_limited, str(e)[:300])
        return None

    percent, reset_dt = report.five_hour(data)
    event = transition(was_limited, percent)
    await repo.save_check(account.id, percent, percent >= 100.0, None)
    if not event or not account.notify_enabled:
        return None
    if event == "limited":
        return Notice(account.owner_tg_id, report.limit_reached_text(account.label, reset_dt))
    return Notice(account.owner_tg_id, report.reset_text(account.label, percent))


async def run_monitor(bot, repo: Repo, http, interval: int, send=None) -> None:
    from app.bot.keyboards import relogin_kb

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(acc: Account):
        async with sem:
            notice = await check_account(http, repo, acc)
        if notice:
            kb = relogin_kb(notice.relogin_account_id) if notice.relogin_account_id else None
            try:
                await bot.send_message(notice.chat_id, notice.text, reply_markup=kb)
            except Exception as e:
                log.warning("не удалось отправить уведомление %s: %s", notice.chat_id, e)

    await asyncio.sleep(5)  # дать боту стартовать
    while True:
        try:
            accounts = await repo.list_active_accounts()
            await asyncio.gather(*(one(a) for a in accounts))
        except Exception:
            log.exception("ошибка цикла мониторинга")
        await asyncio.sleep(interval)
