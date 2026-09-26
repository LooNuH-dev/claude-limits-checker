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


def emoji(percent: float) -> str:
    return "🔴" if percent >= 100 else "🟡" if percent >= 80 else "🟢"


def _limit(usage: dict, key: str) -> tuple[float, datetime | None]:
    block = usage.get(key) or {}
    return float(block.get("utilization") or 0.0), parse_iso(block.get("resets_at"))


def five_hour(usage: dict) -> tuple[float, datetime | None]:
    return _limit(usage, "five_hour")


def _future(dt: datetime | None) -> bool:
    return bool(dt and dt > datetime.now(timezone.utc))


def format_account(label: str, usage: dict | None, error: str | None) -> str:
    lines = [f"👤 <b>{escape(label)}</b>"]
    if error or usage is None:
        lines.append(f"   ⚠️ <i>Ошибка: {escape(error or 'нет данных')}</i>")
        return "\n".join(lines)
    p5, r5 = five_hour(usage)
    lines.append(f"   {emoji(p5)} <b>5-часовой лимит:</b> <code>{p5:.1f}%</code>")
    if p5 >= 100:
        lines.append(f"      • ⏳ Сброс через: <b>{countdown(r5)}</b> (в {local_time(r5)})")
    elif _future(r5):
        lines.append(f"      • ⏳ Сброс в: {local_time(r5)} (через {countdown(r5)})")
    p7, r7 = _limit(usage, "seven_day")
    lines.append(f"   {emoji(p7)} <b>7-дневный лимит:</b> <code>{p7:.1f}%</code>")
    if _future(r7):
        lines.append(f"      • ⏳ Сброс в: {local_time(r7)} (через {countdown(r7)})")
    extra = usage.get("extra_usage") or {}
    if extra.get("is_enabled"):
        pe = float(extra.get("utilization") or 0.0)
        used = float(extra.get("used_credits") or 0.0) / 100
        limit = float(extra.get("monthly_limit") or 0.0) / 100
        lines.append(f"   {emoji(pe)} <b>Extra Usage:</b> <code>{pe:.1f}%</code> (${used:.2f} / ${limit:.2f})")
    return "\n".join(lines)


def format_status(items: list[tuple[str, dict | None, str | None]]) -> str:
    if not items:
        return "⚠️ Нет аккаунтов. Добавьте аккаунт через меню."
    parts = [f"📊 <b>Состояние лимитов Claude Code</b> ({len(items)} акк.)\n"]
    parts += [format_account(label, u, e) + "\n" for label, u, e in items]
    now = datetime.now(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M:%S (UTC+2)")
    parts.append(f"<i>Обновлено: {now}</i>")
    return clip("\n".join(parts))


def limit_reached_text(label: str, reset: datetime | None) -> str:
    return (
        f"⚠️ <b>[{escape(label)}] Достигнут 100% лимит Claude Code!</b>\n\n"
        f"⏳ Сброс через: <b>{countdown(reset)}</b> (в {local_time(reset)})\n\n"
        "🔔 Пришлю уведомление, как только лимит сбросится."
    )


def reset_text(label: str, percent: float) -> str:
    return (
        f"🎉 <b>[{escape(label)}] Лимиты Claude Code сбросились!</b>\n\n"
        f"🟢 5-часовой лимит доступен (использовано: <code>{percent:.1f}%</code>)."
    )


def relogin_text(label: str) -> str:
    return (
        f"🔑 <b>[{escape(label)}] Сессия истекла.</b>\n"
        "Войдите в аккаунт заново, чтобы продолжить мониторинг."
    )
