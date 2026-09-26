from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message


async def safe_edit(message: Message, text: str, **kw):
    """edit_text, но игнорирует безобидную ошибку "message is not modified"."""
    try:
        return await message.edit_text(text, **kw)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return None
        raise
