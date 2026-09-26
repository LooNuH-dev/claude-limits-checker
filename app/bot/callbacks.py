from aiogram.filters.callback_data import CallbackData


class MenuCb(CallbackData, prefix="m"):
    action: str


class AccCb(CallbackData, prefix="a"):
    action: str
    id: int


class AdmCb(CallbackData, prefix="adm"):
    action: str
    tg_id: int = 0
