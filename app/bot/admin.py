from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Filter
from aiogram.types import CallbackQuery

from app.bot import keyboards
from app.bot.callbacks import AdmCb, MenuCb
from app.config import Settings
from app.db import Repo, User

router = Router()


class IsAdmin(Filter):
    async def __call__(self, event, user: User | None = None) -> bool:
        return bool(user and user.is_admin)


router.callback_query.filter(IsAdmin())


@router.callback_query(MenuCb.filter(F.action == "access"))
async def access(cq: CallbackQuery):
    await cq.message.edit_text("👥 <b>Управление доступом</b>", reply_markup=keyboards.admin_menu())
    await cq.answer()


@router.callback_query(AdmCb.filter(F.action == "invite"))
async def invite(cq: CallbackQuery, repo: Repo, user: User, settings: Settings, bot: Bot):
    token = await repo.create_invite(user.tg_id, settings.invite_ttl)
    me = await bot.me()
    hours = settings.invite_ttl // 3600
    await cq.message.answer(
        f"🎟 Одноразовый инвайт (действует {hours} ч):\n"
        f"<code>https://t.me/{me.username}?start=inv_{token}</code>")
    await cq.answer()


@router.callback_query(AdmCb.filter(F.action == "users"))
async def users(cq: CallbackQuery, repo: Repo):
    lst = await repo.list_users()
    lines = ["👥 <b>Пользователи</b>"]
    for u in lst:
        name = f"@{escape(u.username)}" if u.username else str(u.tg_id)
        tag = "👑" if u.is_admin else ("🚫" if u.blocked else "✅")
        n = len(await repo.list_accounts(u.tg_id))
        lines.append(f"{tag} {name} — акк.: {n}")
    await cq.message.edit_text("\n".join(lines), reply_markup=keyboards.users_list(lst))
    await cq.answer()


@router.callback_query(AdmCb.filter(F.action == "revoke"))
async def revoke(cq: CallbackQuery, callback_data: AdmCb):
    await cq.message.edit_text(
        f"🚫 Отозвать доступ у {callback_data.tg_id}? Его аккаунты и токены будут удалены.",
        reply_markup=keyboards.confirm_revoke(callback_data.tg_id))
    await cq.answer()


@router.callback_query(AdmCb.filter(F.action == "confirm_revoke"))
async def confirm_revoke(cq: CallbackQuery, callback_data: AdmCb, repo: Repo):
    ok = await repo.block_user(callback_data.tg_id)
    await cq.answer("Доступ отозван" if ok else "Нельзя отозвать")
    await users(cq, repo)


@router.callback_query(AdmCb.filter(F.action == "diag"))
async def diag(cq: CallbackQuery, repo: Repo):
    s = await repo.stats()
    lines = [
        "🩺 <b>Диагностика</b>",
        f"Пользователей: {s['users']}",
        f"Аккаунтов: {s['accounts']} (требуют входа: {s['needs_relogin']})",
    ]
    if s["errors"]:
        lines.append("\n<b>Последние ошибки:</b>")
        lines += [f"• {escape(label)}: <i>{escape(err)}</i>" for label, err in s["errors"][:20]]
    await cq.message.edit_text("\n".join(lines), reply_markup=keyboards.admin_menu())
    await cq.answer()
