from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.bot.callbacks import AccCb, AdmCb, MenuCb
from app.db import Account, User


def _btn(text: str, cb) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=cb.pack())


def main_menu(is_admin: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.add(_btn("📊 Статус", MenuCb(action="status")))
    b.add(_btn("🗂 Аккаунты", MenuCb(action="accounts")))
    b.add(_btn("➕ Добавить аккаунт", MenuCb(action="add")))
    if is_admin:
        b.add(_btn("👥 Доступ", MenuCb(action="access")))
    b.adjust(2)
    return b.as_markup()


def back_home() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_btn("⬅️ Меню", MenuCb(action="home"))]])


def accounts_list(accounts: list[Account]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for a in accounts:
        mark = "🔑 " if a.needs_relogin else ("🔔 " if a.notify_enabled else "🔕 ")
        b.row(_btn(mark + a.label, AccCb(action="open", id=a.id)))
    b.row(_btn("➕ Добавить", MenuCb(action="add")), _btn("⬅️ Меню", MenuCb(action="home")))
    return b.as_markup()


def account_card(acc: Account) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if acc.needs_relogin:
        b.row(_btn("🔑 Войти заново", AccCb(action="relogin", id=acc.id)))
    b.row(_btn("🔕 Выключить уведомления" if acc.notify_enabled else "🔔 Включить уведомления",
               AccCb(action="notify", id=acc.id)))
    b.row(_btn("✏️ Переименовать", AccCb(action="rename", id=acc.id)),
          _btn("🗑 Удалить", AccCb(action="delete", id=acc.id)))
    b.row(_btn("⬅️ К списку", MenuCb(action="accounts")))
    return b.as_markup()


def confirm_delete(acc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("✅ Да, удалить", AccCb(action="confirm_delete", id=acc_id)),
        _btn("❌ Отмена", AccCb(action="open", id=acc_id)),
    ]])


def relogin_kb(acc_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("🔑 Войти заново", AccCb(action="relogin", id=acc_id))]])


def login_kb(url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔐 Войти в Claude", url=url)],
        [_btn("❌ Отмена", MenuCb(action="cancel"))],
    ])


def admin_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.row(_btn("➕ Создать инвайт", AdmCb(action="invite")))
    b.row(_btn("👥 Пользователи", AdmCb(action="users")))
    b.row(_btn("🩺 Диагностика", AdmCb(action="diag")))
    b.row(_btn("⬅️ Меню", MenuCb(action="home")))
    return b.as_markup()


def users_list(users: list[User]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for u in users:
        if u.is_admin or u.blocked:
            continue
        name = f"@{u.username}" if u.username else str(u.tg_id)
        b.row(_btn(f"🚫 Отозвать {name}", AdmCb(action="revoke", tg_id=u.tg_id)))
    b.row(_btn("⬅️ Назад", MenuCb(action="access")))
    return b.as_markup()


def retry_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("🔁 Начать заново", MenuCb(action="add"))]])


def confirm_revoke(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("✅ Отозвать", AdmCb(action="confirm_revoke", tg_id=tg_id)),
        _btn("❌ Отмена", AdmCb(action="users")),
    ]])
