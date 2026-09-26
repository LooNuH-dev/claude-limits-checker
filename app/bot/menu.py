import asyncio
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import report, usage
from app.bot import keyboards
from app.bot.callbacks import AccCb, MenuCb
from app.bot.start import MENU_TEXT
from app.db import Repo, User

router = Router()


class RenameState(StatesGroup):
    waiting_label = State()


async def _usage_or_error(http, repo, acc):
    if acc.needs_relogin:
        return None, "нужен повторный вход"
    try:
        return await usage.fetch_account_usage(http, repo, acc), None
    except Exception as e:
        return None, str(e)[:200]


@router.callback_query(MenuCb.filter(F.action == "home"))
async def home(cq: CallbackQuery, user: User, state: FSMContext):
    await state.clear()
    await cq.message.edit_text(MENU_TEXT, reply_markup=keyboards.main_menu(user.is_admin))
    await cq.answer()


@router.callback_query(MenuCb.filter(F.action == "status"))
async def status(cq: CallbackQuery, user: User, repo: Repo, http):
    await cq.answer("Проверяю…")
    await cq.message.edit_text("⏳ <i>Проверяю лимиты аккаунтов…</i>")
    accounts = await repo.list_accounts(user.tg_id)
    results = await asyncio.gather(*(_usage_or_error(http, repo, a) for a in accounts))
    items = [(a.label, u, e) for a, (u, e) in zip(accounts, results)]
    await cq.message.edit_text(report.format_status(items), reply_markup=keyboards.back_home())


@router.callback_query(MenuCb.filter(F.action == "accounts"))
async def accounts(cq: CallbackQuery, user: User, repo: Repo):
    accs = await repo.list_accounts(user.tg_id)
    text = "🗂 <b>Ваши аккаунты</b>" if accs else "🗂 Аккаунтов пока нет."
    await cq.message.edit_text(text, reply_markup=keyboards.accounts_list(accs))
    await cq.answer()


async def show_card(message: Message, repo: Repo, http, user: User, acc_id: int, edit: bool = True):
    acc = await repo.get_account(acc_id, owner=user.tg_id)
    if not acc:
        return
    u, e = await _usage_or_error(http, repo, acc)
    acc = await repo.get_account(acc_id, owner=user.tg_id)  # мог стать needs_relogin
    text = report.format_account(acc.label, u, e)
    if acc.email:
        text += f"\n\n📧 {escape(acc.email)}"
    send = message.edit_text if edit else message.answer
    await send(text, reply_markup=keyboards.account_card(acc))


@router.callback_query(AccCb.filter(F.action == "open"))
async def open_card(cq: CallbackQuery, callback_data: AccCb, user: User, repo: Repo, http):
    await cq.answer()
    await show_card(cq.message, repo, http, user, callback_data.id)


@router.callback_query(AccCb.filter(F.action == "notify"))
async def notify(cq: CallbackQuery, callback_data: AccCb, user: User, repo: Repo, http):
    val = await repo.toggle_notify(callback_data.id, user.tg_id)
    await cq.answer("🔔 Уведомления включены" if val else "🔕 Уведомления выключены")
    await show_card(cq.message, repo, http, user, callback_data.id)


@router.callback_query(AccCb.filter(F.action == "delete"))
async def delete(cq: CallbackQuery, callback_data: AccCb):
    await cq.message.edit_text("🗑 Удалить аккаунт и его токены?",
                               reply_markup=keyboards.confirm_delete(callback_data.id))
    await cq.answer()


@router.callback_query(AccCb.filter(F.action == "confirm_delete"))
async def confirm_delete(cq: CallbackQuery, callback_data: AccCb, user: User, repo: Repo):
    await repo.delete_account(callback_data.id, user.tg_id)
    await cq.answer("Удалено")
    accs = await repo.list_accounts(user.tg_id)
    await cq.message.edit_text("🗂 <b>Ваши аккаунты</b>", reply_markup=keyboards.accounts_list(accs))


@router.callback_query(AccCb.filter(F.action == "rename"))
async def rename(cq: CallbackQuery, callback_data: AccCb, state: FSMContext):
    await state.set_state(RenameState.waiting_label)
    await state.update_data(acc_id=callback_data.id)
    await cq.message.answer("✏️ Пришлите новое название (до 64 символов).")
    await cq.answer()


@router.message(RenameState.waiting_label, F.text)
async def rename_done(message: Message, state: FSMContext, user: User, repo: Repo, http):
    data = await state.get_data()
    await state.clear()
    label = message.text.strip()[:64]
    if label:
        await repo.rename_account(data["acc_id"], user.tg_id, label)
    await show_card(message, repo, http, user, data["acc_id"], edit=False)
