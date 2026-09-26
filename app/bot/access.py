from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.db import Repo

INVITE_PREFIX = "/start inv_"


class AccessMiddleware(BaseMiddleware):
    """Пропускает только известных незаблокированных пользователей.

    Единственное исключение — `/start inv_<token>`: его проверяет обработчик,
    и при невалидном токене тоже молчит.
    """

    def __init__(self, repo: Repo):
        self.repo = repo

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user is None:
            return None
        user = await self.repo.get_user(from_user.id)
        if user and not user.blocked:
            data["user"] = user
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith(INVITE_PREFIX):
            data["user"] = None
            return await handler(event, data)
        return None
