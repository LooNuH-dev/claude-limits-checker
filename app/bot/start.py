from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.bot import keyboards
from app.db import Repo, User

router = Router()
MENU_TEXT = "🤖 <b>Claude Limits Checker</b>\nВыберите действие:"


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, repo: Repo, user: User | None):
    if user is None:
        args = command.args or ""
        if not args.startswith("inv_"):
            return
        ok = await repo.redeem_invite(args.removeprefix("inv_"), message.from_user.id,
                                      message.from_user.username)
        if not ok:
            return  # невалидный инвайт — молчим
        user = await repo.get_user(message.from_user.id)
        await message.answer("✅ Доступ выдан. Добавьте аккаунт Claude, чтобы следить за лимитами.")
    await message.answer(MENU_TEXT, reply_markup=keyboards.main_menu(user.is_admin))
