import os
from dataclasses import dataclass
from typing import Mapping

REQUIRED = ("BOT_TOKEN", "ADMIN_TELEGRAM_ID", "ENCRYPTION_KEY")


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_id: int
    encryption_key: str
    db_path: str
    check_interval: int
    invite_ttl: int


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise RuntimeError("Не заданы переменные окружения: " + ", ".join(missing))
    return Settings(
        bot_token=env["BOT_TOKEN"].strip(),
        admin_id=int(env["ADMIN_TELEGRAM_ID"]),
        encryption_key=env["ENCRYPTION_KEY"].strip(),
        db_path=env.get("DB_PATH", "/data/bot.db"),
        check_interval=max(60, int(env.get("CHECK_INTERVAL", "300"))),
        invite_ttl=int(env.get("INVITE_TTL", "86400")),
    )
