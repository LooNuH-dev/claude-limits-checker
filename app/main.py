import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.bot import add_account, admin, menu, start
from app.bot.access import AccessMiddleware
from app.config import load_settings
from app.crypto import Cipher
from app.db import Repo
from app.monitor import run_monitor


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    repo = await Repo.open(settings.db_path, Cipher(settings.encryption_key))
    await repo.upsert_admin(settings.admin_id)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    http = aiohttp.ClientSession()
    dp["repo"], dp["http"], dp["settings"] = repo, http, settings

    access = AccessMiddleware(repo)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    # add_account раньше menu: его FSM-хендлер кода должен ловить текст первым
    dp.include_routers(start.router, admin.router, add_account.router, menu.router)

    await bot.delete_webhook(drop_pending_updates=True)
    monitor = asyncio.create_task(run_monitor(bot, repo, http, settings.check_interval))
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        monitor.cancel()
        await http.close()
        await repo.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
