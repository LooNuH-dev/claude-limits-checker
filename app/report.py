from datetime import datetime, timedelta, timezone
from html import escape

DISPLAY_TZ = timezone(timedelta(hours=2))


def clip(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def countdown(target: datetime | None) -> str:
    if not target:
        return "неизвестно"
    total = int((target - datetime.now(timezone.utc)).total_seconds())
    if total <= 0:
        return "уже сброшен"
    days, hours, minutes = total // 86400, (total % 86400) // 3600, (total % 3600) // 60
    parts = []
    if days:
        parts.append(f"{days} дн")
    if hours or days:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)


def local_time(target: datetime | None) -> str:
    if not target:
        return "—"
    return target.astimezone(DISPLAY_TZ).strftime("%H:%M (%d.%m UTC+2)")


def _limit(usage: dict, key: str) -> tuple[float, datetime | None]:
    block = usage.get(key) or {}
    return float(block.get("utilization") or 0.0), parse_iso(block.get("resets_at"))


def five_hour(usage: dict) -> tuple[float, datetime | None]:
    return _limit(usage, "five_hour")


def seven_day(usage: dict) -> tuple[float, datetime | None]:
    return _limit(usage, "seven_day")


def _future(dt: datetime | None) -> bool:
    return bool(dt and dt > datetime.now(timezone.utc))


def _reset_line(reset: datetime | None) -> str | None:
    if not _future(reset):
        return None
    return f"  сброс в {local_time(reset)}, через {countdown(reset)}"


def format_account(label: str, usage: dict | None, error: str | None) -> str:
    lines = [f"<b>{escape(label)}</b>"]
    if error or usage is None:
        lines.append(f"Ошибка: {escape(error or 'нет данных')}")
        return "\n".join(lines)
    for name, (p, r) in (("5 часов", five_hour(usage)), ("7 дней", seven_day(usage))):
        lines.append(f"{name}: {p:.1f}%")
        if (reset := _reset_line(r)):
            lines.append(reset)
    extra = usage.get("extra_usage") or {}
    if extra.get("is_enabled"):
        pe = float(extra.get("utilization") or 0.0)
        used = float(extra.get("used_credits") or 0.0) / 100
        limit = float(extra.get("monthly_limit") or 0.0) / 100
        lines.append(f"Extra Usage: {pe:.1f}% (${used:.2f} из ${limit:.2f})")
    return "\n".join(lines)


def format_status(items: list[tuple[str, dict | None, str | None]]) -> str:
    if not items:
        return "Нет аккаунтов. Добавьте аккаунт через меню."
    parts = [format_account(label, u, e) for label, u, e in items]
    now = datetime.now(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M (UTC+2)")
    parts.append(f"Обновлено: {now}")
    return clip("\n\n".join(parts))


def limit_reached_text(label: str, reset: datetime | None) -> str:
    return (
        f"🔴 [{escape(label)}] 5-часовой лимит: 100%.\n"
        f"Сброс в {local_time(reset)}, через {countdown(reset)}."
    )


def reset_text(label: str, percent: float) -> str:
    return f"🟢 [{escape(label)}] 5-часовой лимит сбросился, использовано {percent:.1f}%."


def weekly_limit_reached_text(label: str, reset: datetime | None) -> str:
    return (
        f"🔴 [{escape(label)}] Недельный лимит: 100%.\n"
        f"Сброс в {local_time(reset)}, через {countdown(reset)}."
    )


def weekly_reset_text(label: str, percent: float) -> str:
    return f"🟢 [{escape(label)}] Недельный лимит сбросился, использовано {percent:.1f}%."


def relogin_text(label: str) -> str:
    return f"🔑 [{escape(label)}] Сессия истекла, мониторинг остановлен. Нужен повторный вход."
