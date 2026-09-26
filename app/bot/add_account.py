from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import oauth
from app.bot import keyboards
from app.bot.callbacks import AccCb, MenuCb
from app.bot.menu import show_card
from app.bot.start import MENU_TEXT
from app.db import Repo, User

router = Router()


class AddState(StatesGroup):
    waiting_code = State()


INSTRUCTIONS = (
    "🔐 <b>Вход в Claude</b>\n\n"
    "1. Нажмите «Войти в Claude» и авторизуйтесь нужным аккаунтом.\n"
    "2. На странице с кодом нажмите <b>Copy Code</b>.\n"
    "3. Вставьте код сюда — сообщение будет сразу удалено.\n\n"
    "<i>Ссылка действует 10 минут.</i>"
)


async def _begin(message: Message, state: FSMContext, repo: Repo, user: User,
                 account_id: int | None):
    verifier, challenge = oauth.make_pkce()
    st = oauth.new_state()
    await repo.save_pending(st, user.tg_id, verifier, account_id)
    await state.set_state(AddState.waiting_code)
    await state.update_data(oauth_state=st)
    await message.answer(INSTRUCTIONS,
                         reply_markup=keyboards.login_kb(oauth.build_authorize_url(challenge, st)))


@router.callback_query(MenuCb.filter(F.action == "add"))
async def add(cq: CallbackQuery, state: FSMContext, repo: Repo, user: User):
    await cq.answer()
    await _begin(cq.message, state, repo, user, None)


@router.callback_query(AccCb.filter(F.action == "relogin"))
async def relogin(cq: CallbackQuery, callback_data: AccCb, state: FSMContext, repo: Repo, user: User):
    await cq.answer()
    if await repo.get_account(callback_data.id, owner=user.tg_id):
        await _begin(cq.message, state, repo, user, callback_data.id)


@router.callback_query(MenuCb.filter(F.action == "cancel"))
async def cancel(cq: CallbackQuery, state: FSMContext, user: User):
    await state.clear()
    await cq.message.edit_text(MENU_TEXT, reply_markup=keyboards.main_menu(user.is_admin))
    await cq.answer("Отменено")


@router.message(AddState.waiting_code, F.text)
async def got_code(message: Message, state: FSMContext, repo: Repo, user: User, http, bot: Bot):
    try:
        await message.delete()  # код не должен оставаться в чате
    except Exception:
        pass
    data = await state.get_data()
    expected = data.get("oauth_state")
    retry = keyboards.retry_kb()
    try:
        code, st = oauth.parse_code_input(message.text)
    except ValueError:
        await message.answer("⚠️ Не похоже на код. Вставьте код со страницы Claude.")
        return
    if st and st != expected:
        await message.answer("⚠️ Код от другой попытки входа.", reply_markup=retry)
        return
    await state.clear()
    pending = await repo.pop_pending(expected, user.tg_id)
    if not pending:
        await message.answer("⌛ Время входа истекло.", reply_markup=retry)
        return
    verifier, account_id = pending
    status = await message.answer("⏳ <i>Подключаю аккаунт…</i>")
    try:
        ts = await oauth.exchange_code(http, code, expected, verifier)
    except oauth.OAuthError as e:
        await status.edit_text(f"⚠️ Не удалось войти: {e}", reply_markup=retry)
        return
    if account_id:
        await repo.update_tokens(account_id, ts.access_token, ts.refresh_token, ts.expires_at)
    else:
        count = len(await repo.list_accounts(user.tg_id))
        label = ts.email or f"Аккаунт {count + 1}"
        account_id = await repo.add_account(user.tg_id, label, ts.email, ts.access_token,
                                            ts.refresh_token, ts.expires_at)
    await status.edit_text("✅ Аккаунт подключён.")
    await show_card(message, repo, http, user, account_id, edit=False)
