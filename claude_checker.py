#!/usr/bin/env python3
"""
Claude Code Limits Checker & Telegram Notifier
----------------------------------------------
Multi-Account & Remote Server / Coolify / Docker Support.
Часовой пояс: UTC+2.
"""

import os
import sys
import json
import time
import re
import platform
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import argparse
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_KEYCHAIN_SERVICE = "Claude Code-credentials"
API_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_REFRESH_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USER_AGENT = "claude-code/0.2.29"

IS_MACOS = platform.system() == "Darwin"

# Часовой пояс UTC+2
TZ_OFFSET_HOURS = 2
DISPLAY_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

# --- Config Management (File + Coolify ENV) ---

def load_config():
    cfg = {
        "bot_token": "",
        "chat_id": "",
        "check_interval_minutes": 5,
        "notify_on_reset": True,
        "notify_on_limit_reached": True,
        "auto_discover_keychain": IS_MACOS,
        "accounts": []
    }

    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                cfg.update(file_cfg)
        except Exception as e:
            print(f"⚠️ Ошибка чтения {CONFIG_FILE}: {e}")

    env_config_json = os.environ.get("CONFIG_JSON")
    if env_config_json:
        try:
            parsed = json.loads(env_config_json)
            cfg.update(parsed)
        except Exception as e:
            print(f"⚠️ Ошибка разбора CONFIG_JSON из ENV: {e}")

    if os.environ.get("BOT_TOKEN"):
        cfg["bot_token"] = os.environ.get("BOT_TOKEN").strip()
    if os.environ.get("CHAT_ID"):
        cfg["chat_id"] = os.environ.get("CHAT_ID").strip()
    if os.environ.get("CHECK_INTERVAL_MINUTES"):
        try:
            cfg["check_interval_minutes"] = int(os.environ.get("CHECK_INTERVAL_MINUTES"))
        except ValueError:
            pass

    env_accounts = os.environ.get("ACCOUNTS_JSON")
    if env_accounts:
        try:
            cfg["accounts"] = json.loads(env_accounts)
        except Exception as e:
            print(f"⚠️ Ошибка разбора ACCOUNTS_JSON из ENV: {e}")

    if "accounts" not in cfg:
        cfg["accounts"] = []

    return cfg

def save_config(config):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_primary_email():
    p = os.path.expanduser("~/.claude.json")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d.get("oauthAccount", {}).get("emailAddress")
        except Exception:
            pass
    return None

# --- Keychain (macOS) ---

def discover_keychain_services():
    if not IS_MACOS:
        return []
    services = []
    try:
        res = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True, errors="ignore")
        matches = re.findall(r'\"svce\"<blob>=\"([^\"]*Claude Code-credentials[^\"]*)\"', res.stdout)
        services = sorted(list(set(matches)))
    except Exception:
        pass

    if not services and is_keychain_service_exists(DEFAULT_KEYCHAIN_SERVICE):
        services = [DEFAULT_KEYCHAIN_SERVICE]
    return services

def is_keychain_service_exists(service_name):
    if not IS_MACOS:
        return False
    try:
        res = subprocess.run(["security", "find-generic-password", "-s", service_name], capture_output=True, text=True)
        return res.returncode == 0
    except Exception:
        return False

def get_keychain_credentials(service_name=DEFAULT_KEYCHAIN_SERVICE):
    if not IS_MACOS:
        raise RuntimeError("Keychain доступен только на macOS")
    try:
        res = subprocess.run(
            ["security", "find-generic-password", "-s", service_name, "-w"],
            capture_output=True, text=True, check=True
        )
        data = json.loads(res.stdout.strip())
        oauth = data.get("claudeAiOauth", {})
        return oauth.get("accessToken", ""), oauth.get("refreshToken", ""), oauth.get("scopes", [])
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать Keychain ({service_name}): {e}")

def update_keychain_credentials(service_name, new_access_token, new_refresh_token):
    if not IS_MACOS:
        return
    try:
        res = subprocess.run(
            ["security", "find-generic-password", "-s", service_name, "-w"],
            capture_output=True, text=True, check=True
        )
        data = json.loads(res.stdout.strip())
        if "claudeAiOauth" in data:
            data["claudeAiOauth"]["accessToken"] = new_access_token
            data["claudeAiOauth"]["refreshToken"] = new_refresh_token
            new_json_str = json.dumps(data)
            subprocess.run(
                ["security", "add-generic-password", "-U", "-s", service_name, "-w", new_json_str],
                capture_output=True, check=True
            )
    except Exception:
        pass

def refresh_claude_cli_token():
    if not IS_MACOS:
        return None
    try:
        subprocess.run(["claude", "-p", "hi"], capture_output=True, timeout=10)
        token, _, _ = get_keychain_credentials(DEFAULT_KEYCHAIN_SERVICE)
        return token
    except Exception as e:
        raise RuntimeError(f"Не удалось обновить токен через Claude CLI: {e}")

# --- Direct OAuth Refresh (Coolify / Remote VPS / Docker) ---

def refresh_oauth_token_direct(refresh_token):
    """Прямое обновление OAuth токена через API Anthropic."""
    if not refresh_token or not refresh_token.strip():
        raise ValueError("Токен ротации отсутствует. Выполните 'claude auth login' на Mac и повторите экспорт.")

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_REFRESH_URL,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            new_access_token = res_json.get("access_token")
            new_refresh_token = res_json.get("refresh_token") or refresh_token
            return new_access_token, new_refresh_token
    except urllib.error.HTTPError as e:
        err_msg = ""
        try:
            err_body = json.loads(e.read().decode("utf-8"))
            err_msg = err_body.get("error", {}).get("message") or err_body.get("error") or str(err_body)
        except Exception:
            pass

        if e.code == 400 or "invalid_grant" in str(err_msg).lower():
            raise RuntimeError("Токен аннулирован или истек. Требуется выполнить 'claude auth login' на Mac и экспортировать CONFIG_JSON.")
        raise RuntimeError(f"HTTP {e.code}: {err_msg or e.reason}")

# --- Account Usage Fetcher ---

def get_active_accounts(config):
    configured = config.get("accounts", [])
    if not IS_MACOS or not config.get("auto_discover_keychain", True):
        return configured

    discovered_services = discover_keychain_services()
    accounts = list(configured)
    existing_services = {a.get("keychain_service") for a in configured if a.get("type") == "keychain"}
    primary_email = get_primary_email()

    for svc in discovered_services:
        if svc not in existing_services:
            if svc == DEFAULT_KEYCHAIN_SERVICE:
                label = f"Основной ({primary_email})" if primary_email else "Основной аккаунт"
            else:
                label = f"Аккаунт ({svc.replace('Claude Code-credentials-', '')})"

            accounts.append({
                "id": svc,
                "name": label,
                "type": "keychain",
                "keychain_service": svc
            })

    if not accounts and IS_MACOS:
        accounts.append({
            "id": DEFAULT_KEYCHAIN_SERVICE,
            "name": f"Основной ({primary_email})" if primary_email else "Основной аккаунт",
            "type": "keychain",
            "keychain_service": DEFAULT_KEYCHAIN_SERVICE
        })

    return accounts

def fetch_usage_for_token(token):
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json"
    }
    req = urllib.request.Request(API_USAGE_URL, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))

def fetch_account_usage(account, config):
    acc_type = account.get("type", "keychain" if IS_MACOS else "token")

    try:
        if acc_type == "keychain" and IS_MACOS:
            svc = account.get("keychain_service", DEFAULT_KEYCHAIN_SERVICE)
            token, rf_token, _ = get_keychain_credentials(svc)

            if not token and not rf_token:
                return None, f"Токен отсутствует в Keychain ({svc}). Запустите 'claude auth login'"

            try:
                data = fetch_usage_for_token(token)
                return data, None
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    return None, "Превышен лимит частых запросов к API (HTTP 429). Ожидайте автоматического обновления."
                if e.code == 401:
                    if rf_token:
                        try:
                            new_acc, new_rf = refresh_oauth_token_direct(rf_token)
                            update_keychain_credentials(svc, new_acc, new_rf)
                            data = fetch_usage_for_token(new_acc)
                            return data, None
                        except Exception:
                            pass
                    if svc == DEFAULT_KEYCHAIN_SERVICE:
                        token = refresh_claude_cli_token()
                        if token:
                            data = fetch_usage_for_token(token)
                            return data, None
                raise e

        elif acc_type == "token":
            token = account.get("access_token")
            rf_token = account.get("refresh_token")

            if not token and not rf_token:
                return None, "Токены отсутствуют в конфиге. Выполните 'claude auth login' и повторите export"

            if token:
                try:
                    data = fetch_usage_for_token(token)
                    return data, None
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        return None, "Превышен лимит частых запросов к API (HTTP 429). Ожидайте следующей проверки."
                    if e.code != 401 or not rf_token:
                        raise e

            if rf_token:
                try:
                    new_acc, new_rf = refresh_oauth_token_direct(rf_token)
                    account["access_token"] = new_acc
                    account["refresh_token"] = new_rf
                    save_config(config)

                    data = fetch_usage_for_token(new_acc)
                    return data, None
                except Exception as refresh_err:
                    return None, f"{refresh_err}"

        else:
            return None, f"Неподдерживаемый тип аккаунта ({acc_type}) для этой ОС"

    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None, "Превышен лимит частых запросов к API (HTTP 429). Ожидайте следующей проверки."
        if e.code == 401:
            return None, "Истек срок действия токена (HTTP 401 Требуется повторный вход)"
        return None, f"Ошибка API Anthropic (HTTP {e.code}): {e.reason}"
    except Exception as e:
        return None, str(e)

def fetch_all_accounts_usage(config):
    accounts = get_active_accounts(config)
    results = []
    for acc in accounts:
        usage, err = fetch_account_usage(acc, config)
        results.append({
            "account": acc,
            "usage": usage,
            "error": err
        })
    return results

# --- Export Config for Coolify & Remote VPS ---

def export_config():
    config = load_config()
    export_accounts = []
    seen_refresh_tokens = set()
    primary_email = get_primary_email()

    custom_names = {}
    for acc in config.get("accounts", []):
        svc = acc.get("keychain_service")
        if svc and acc.get("name"):
            custom_names[svc] = acc.get("name")

    # 1. Читаем из Keychain на macOS
    if IS_MACOS:
        discovered = discover_keychain_services()
        for svc in discovered:
            try:
                acc_token, rf_token, scopes = get_keychain_credentials(svc)
                if acc_token or rf_token:
                    if svc in custom_names:
                        label = custom_names[svc]
                    elif svc == DEFAULT_KEYCHAIN_SERVICE:
                        label = f"Основной ({primary_email})" if primary_email else "Основной аккаунт"
                    else:
                        label = f"Аккаунт ({svc.replace('Claude Code-credentials-', '')})"

                    export_accounts.append({
                        "name": label,
                        "type": "token",
                        "access_token": acc_token or "",
                        "refresh_token": rf_token or "",
                        "scopes": scopes or ["user:inference", "user:profile"]
                    })
                    if rf_token:
                        seen_refresh_tokens.add(rf_token)
            except Exception as e:
                print(f"⚠️ Предупреждение при чтении Keychain ({svc}): {e}")

    # 2. Читаем вручную добавленные аккаунты из config.json
    for acc in config.get("accounts", []):
        acc_token = acc.get("access_token") or ""
        rf_token = acc.get("refresh_token") or ""

        if acc.get("type") == "token" and (acc_token or rf_token):
            if rf_token not in seen_refresh_tokens:
                export_accounts.append({
                    "name": acc.get("name", "Доп. аккаунт"),
                    "type": "token",
                    "access_token": acc_token,
                    "refresh_token": rf_token,
                    "scopes": acc.get("scopes", ["user:inference", "user:profile"])
                })
                if rf_token:
                    seen_refresh_tokens.add(rf_token)

    payload = {
        "bot_token": config.get("bot_token", ""),
        "chat_id": config.get("chat_id", ""),
        "check_interval_minutes": config.get("check_interval_minutes", 5),
        "notify_on_reset": config.get("notify_on_reset", True),
        "notify_on_limit_reached": config.get("notify_on_limit_reached", True),
        "auto_discover_keychain": False,
        "accounts": export_accounts
    }

    return payload

# --- Helper Formatting Functions (UTC+2) ---

def parse_iso_time(iso_str):
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except Exception:
        return None

def format_countdown(target_dt):
    if not target_dt:
        return "неизвестно"
    now_dt = datetime.now(timezone.utc)
    diff = target_dt - now_dt
    total_sec = int(diff.total_seconds())
    if total_sec <= 0:
        return "уже сброшен"

    days = total_sec // 86400
    hours = (total_sec % 86400) // 3600
    minutes = (total_sec % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days} дн")
    if hours > 0 or days > 0:
        parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)

def format_local_time(target_dt):
    """Форматирует время в часовом поясе UTC+2"""
    if not target_dt:
        return "—"
    dt_tz = target_dt.astimezone(DISPLAY_TZ)
    return dt_tz.strftime("%H:%M (%d.%m UTC+2)")

def get_status_emoji(percent):
    if percent >= 100:
        return "🔴"
    elif percent >= 80:
        return "🟡"
    else:
        return "🟢"

# --- Output Formatters ---

def format_status_report(results):
    if not results:
        return "⚠️ Нет настроенных аккаунтов для проверки."

    lines = [f"📊 *Состояние лимитов Claude Code* ({len(results)} акк.)\n"]

    for idx, item in enumerate(results, 1):
        acc = item["account"]
        acc_name = acc.get("name") or f"Аккаунт #{idx}"
        data = item["usage"]
        err = item["error"]

        lines.append(f"👤 *{acc_name}*")

        if err:
            lines.append(f"   ⚠️ _Ошибка: {err}_\n")
            continue

        five_h = data.get("five_hour", {})
        p5 = five_h.get("utilization", 0.0) or 0.0
        reset5_dt = parse_iso_time(five_h.get("resets_at"))
        emoji5 = get_status_emoji(p5)

        lines.append(f"   {emoji5} *5-часовой лимит:* `{p5:.1f}%`")
        if p5 >= 100:
            lines.append(f"      • ⏳ Сброс через: *{format_countdown(reset5_dt)}* (в {format_local_time(reset5_dt)})")
        elif reset5_dt and (reset5_dt - datetime.now(timezone.utc)).total_seconds() > 0:
            lines.append(f"      • ⏳ Сброс в: {format_local_time(reset5_dt)} (через {format_countdown(reset5_dt)})")

        seven_d = data.get("seven_day", {})
        p7 = seven_d.get("utilization", 0.0) or 0.0
        reset7_dt = parse_iso_time(seven_d.get("resets_at"))
        emoji7 = get_status_emoji(p7)

        lines.append(f"   {emoji7} *7-дневный лимит:* `{p7:.1f}%`")
        if reset7_dt and (reset7_dt - datetime.now(timezone.utc)).total_seconds() > 0:
            lines.append(f"      • ⏳ Сброс в: {format_local_time(reset7_dt)} (через {format_countdown(reset7_dt)})")

        extra = data.get("extra_usage") or {}
        if extra.get("is_enabled"):
            p_ext = extra.get("utilization", 0.0) or 0.0
            used_c = extra.get("used_credits", 0.0) or 0.0
            limit_c = extra.get("monthly_limit", 0.0) or 0.0
            emoji_ext = get_status_emoji(p_ext)
            lines.append(f"   {emoji_ext} *Extra Usage:* `{p_ext:.1f}%` (${used_c / 100:.2f} / ${limit_c / 100:.2f})")

        lines.append("")

    now_str = datetime.now(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M:%S (UTC+2)")
    lines.append(f"_Обновлено: {now_str}_")

    return "\n".join(lines)

def format_console_report(results):
    if not results:
        return "⚠️ Нет настроенных аккаунтов для проверки."

    lines = ["=" * 50, f"   СОСТОЯНИЕ ЛИМИТОВ CLAUDE CODE ({len(results)} акк.) [UTC+2]", "=" * 50]

    for idx, item in enumerate(results, 1):
        acc = item["account"]
        acc_name = acc.get("name") or f"Аккаунт #{idx}"
        data = item["usage"]
        err = item["error"]

        lines.append(f"\n👤 [{acc_name}]")

        if err:
            lines.append(f"  ❌ Ошибка: {err}")
            continue

        five_h = data.get("five_hour", {})
        p5 = five_h.get("utilization", 0.0) or 0.0
        reset5_dt = parse_iso_time(five_h.get("resets_at"))

        lines.append(f"  • 5-часовой лимит: {p5:.1f}%")
        if p5 >= 100:
            lines.append(f"    --> ЛИМИТ ДОСТИГНУТ! Сброс через: {format_countdown(reset5_dt)} (в {format_local_time(reset5_dt)})")
        else:
            lines.append(f"    --> Сброс в: {format_local_time(reset5_dt)} (через {format_countdown(reset5_dt)})")

        seven_d = data.get("seven_day", {})
        p7 = seven_d.get("utilization", 0.0) or 0.0
        reset7_dt = parse_iso_time(seven_d.get("resets_at"))

        lines.append(f"  • 7-дневный лимит: {p7:.1f}%")
        lines.append(f"    --> Сброс в: {format_local_time(reset7_dt)} (через {format_countdown(reset7_dt)})")

        extra = data.get("extra_usage") or {}
        if extra.get("is_enabled"):
            p_ext = extra.get("utilization", 0.0) or 0.0
            used_c = extra.get("used_credits", 0.0) or 0.0
            limit_c = extra.get("monthly_limit", 0.0) or 0.0
            lines.append(f"  • Extra Usage: {p_ext:.1f}% (${used_c / 100:.2f} / ${limit_c / 100:.2f})")

    lines.append("\n" + "=" * 50)
    return "\n".join(lines)

# --- Telegram API ---

def send_telegram_message(bot_token, chat_id, text):
    if not bot_token or not chat_id:
        print("⚠️ Bot token или Chat ID не настроены!")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            return res_json.get("ok", False)
    except Exception as e:
        print(f"⚠️ Ошибка отправки сообщения в Telegram: {e}")
        return False

def telegram_get_updates(bot_token, offset=None):
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates?timeout=5"
    if offset:
        url += f"&offset={offset}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

# --- Setup Wizard ---

def run_setup():
    print("\n⚙️ НАСТРОЙКА TELEGRAM БОТА CLAUDE LIMITS CHECKER")
    print("-" * 50)
    config = load_config()

    print("1. Создайте бота в Telegram через @BotFather и скопируйте HTTP API Token.")
    bot_token = input(f"Введите Bot Token [{config.get('bot_token', '')}]: ").strip()
    if bot_token:
        config["bot_token"] = bot_token

    if not config["bot_token"]:
        print("❌ Bot Token обязателен!")
        return

    print("\n2. Отправьте любое сообщение вашему боту в Telegram.")
    print("   Ожидание сообщения для автоматического получения Chat ID...")

    offset = None
    chat_id = None
    start_time = time.time()

    while time.time() - start_time < 60:
        updates = telegram_get_updates(config["bot_token"], offset)
        if updates and updates.get("ok") and updates.get("result"):
            for up in updates["result"]:
                offset = up["update_id"] + 1
                if "message" in up:
                    msg = up["message"]
                    chat_id = str(msg["chat"]["id"])
                    user_name = msg["from"].get("first_name", "Пользователь")
                    print(f"\n✅ Сообщение получено от {user_name}! Chat ID: {chat_id}")
                    break
        if chat_id:
            break
        time.sleep(2)

    if not chat_id:
        manual_id = input("Chat ID не найден автоматически. Введите Chat ID вручную (или нажмите Enter): ").strip()
        if manual_id:
            chat_id = manual_id

    if chat_id:
        config["chat_id"] = chat_id
        save_config(config)
        print("\n🎉 Настройка Telegram завершена успешно!")
        if send_telegram_message(config["bot_token"], config["chat_id"], "🤖 *Claude Limits Checker* успешно настроен и готов к работе!"):
            print("✅ Тестовое сообщение отправлено в Telegram!")
        else:
            print("❌ Ошибка при отправке тестового сообщения.")
    else:
        print("❌ Chat ID не установлен.")

def manage_accounts():
    config = load_config()
    print("\n👤 УПРАВЛЕНИЕ АККАУНТАМИ CLAUDE CODE")
    print("-" * 50)

    active_accounts = get_active_accounts(config)
    print(f"Текущие отслеживаемые аккаунты ({len(active_accounts)}):")
    for idx, acc in enumerate(active_accounts, 1):
        a_type = acc.get("type")
        detail = f"Keychain: {acc.get('keychain_service')}" if a_type == "keychain" else "Прямой токен"
        has_t = "OK" if (acc.get("access_token") or acc.get("refresh_token") or a_type == "keychain") else "НЕТ ТОКЕНА"
        print(f"  {idx}. {acc.get('name')} [{detail}] [{has_t}]")

    print("\nДействия:")
    if IS_MACOS:
        print("  1. Добавить новый аккаунт из Keychain")
    print("  2. Добавить новый аккаунт по OAuth Токену (Access & Refresh)")
    print("  3. Переименовать аккаунт")
    print("  4. Удалить аккаунт")
    if IS_MACOS:
        print("  5. Переключить автообнаружение Keychain (сейчас: {})".format("Вкл" if config.get("auto_discover_keychain", True) else "Выкл"))
    print("  0. Выход")

    choice = input("\nВыберите действие: ").strip()

    if choice == "1" and IS_MACOS:
        svcs = discover_keychain_services()
        print("\nОбнаруженные Keychain записи:")
        for idx, s in enumerate(svcs, 1):
            print(f"  {idx}. {s}")
        print(f"  {len(svcs) + 1}. Ввести имя Keychain записи вручную")

        sel = input("Выберите запись: ").strip()
        if sel.isdigit():
            s_idx = int(sel) - 1
            if 0 <= s_idx < len(svcs):
                svc_name = svcs[s_idx]
            else:
                svc_name = input("Введите имя Keychain записи: ").strip()

            acc_label = input(f"Введите название для этого аккаунта (напр. 'Рабочий') [{svc_name}]: ").strip() or svc_name
            config["accounts"].append({
                "id": svc_name,
                "name": acc_label,
                "type": "keychain",
                "keychain_service": svc_name
            })
            save_config(config)
            print(f"✅ Аккаунт '{acc_label}' добавлен!")

    elif choice == "2":
        acc_name = input("Введите название аккаунта (напр. 'Личный' / 'Рабочий'): ").strip() or "Удаленный аккаунт"
        acc_token = input("Введите Access Token (sk-ant-oat01-...): ").strip()
        rf_token = input("Введите Refresh Token (sk-ant-ort01-...): ").strip()

        if acc_token or rf_token:
            config["accounts"].append({
                "id": f"token_{int(time.time())}",
                "name": acc_name,
                "type": "token",
                "access_token": acc_token,
                "refresh_token": rf_token
            })
            save_config(config)
            print(f"✅ Аккаунт '{acc_name}' с токенами добавлен в config.json!")

    elif choice == "3":
        if not active_accounts:
            print("Нет доступных аккаунтов.")
            return
        sel = input("Введите номер аккаунта для переименования: ").strip()
        if sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(active_accounts):
                target_acc = active_accounts[idx]
                new_name = input(f"Введите новое название для '{target_acc.get('name')}': ").strip()
                if new_name:
                    found = False
                    for acc in config.get("accounts", []):
                        if acc.get("keychain_service") == target_acc.get("keychain_service") or acc.get("id") == target_acc.get("id"):
                            acc["name"] = new_name
                            found = True
                            break
                    if not found:
                        config["accounts"].append({
                            "id": target_acc.get("keychain_service") or target_acc.get("id"),
                            "name": new_name,
                            "type": target_acc.get("type", "keychain"),
                            "keychain_service": target_acc.get("keychain_service")
                        })
                    save_config(config)
                    print(f"✅ Название изменено на '{new_name}'!")

    elif choice == "4":
        if not config.get("accounts"):
            print("У вас нет настроенных вручную аккаунтов для удаления.")
            return
        sel = input("Введите номер аккаунта для удаления: ").strip()
        if sel.isdigit():
            idx = int(sel) - 1
            if 0 <= idx < len(config["accounts"]):
                removed = config["accounts"].pop(idx)
                save_config(config)
                print(f"✅ Аккаунт '{removed.get('name')}' удален.")

    elif choice == "5" and IS_MACOS:
        config["auto_discover_keychain"] = not config.get("auto_discover_keychain", True)
        save_config(config)
        print("✅ Автообнаружение Keychain переключено на: {}".format("Вкл" if config["auto_discover_keychain"] else "Выкл"))

# --- Bot & Monitor Daemon ---

def run_daemon():
    config = load_config()
    bot_token = config.get("bot_token")
    chat_id = config.get("chat_id")

    if not bot_token or not chat_id:
        print("❌ Ошибка: Скрипт не настроен! Задайте переменные окружения BOT_TOKEN и CHAT_ID в Coolify или запустите: python3 claude_checker.py setup")
        sys.exit(1)

    print("🚀 Запуск фонового демона мониторинга (Multi-Account & Coolify) и Telegram-бота...")
    print(f"   Частота проверки: каждые {config.get('check_interval_minutes', 5)} мин. Часовой пояс: UTC+2.")

    send_telegram_message(
        bot_token, chat_id,
        "🚀 *Claude Limits Checker запущен в контейнере!*\n"
        "Отправьте `/status` или `/check` для получения текущего состояния всех аккаунтов."
    )

    offset = None
    last_check_time = 0
    check_interval = config.get("check_interval_minutes", 5) * 60

    account_states = config.get("account_states", {})

    while True:
        now = time.time()

        # 1. Периодическая проверка лимитов для всех аккаунтов
        if now - last_check_time >= check_interval:
            last_check_time = now
            try:
                results = fetch_all_accounts_usage(config)

                for item in results:
                    acc = item["account"]
                    acc_id = acc.get("keychain_service") or acc.get("id") or acc.get("name")
                    acc_name = acc.get("name", "Аккаунт")
                    data = item["usage"]

                    if not data:
                        continue

                    five_h = data.get("five_hour", {})
                    p5 = five_h.get("utilization", 0.0) or 0.0
                    reset5_dt = parse_iso_time(five_h.get("resets_at"))

                    was_limited = account_states.get(acc_id, False)
                    is_limited = p5 >= 100.0

                    # Переход в состояние "Лимит 100%"
                    if is_limited and not was_limited and config.get("notify_on_limit_reached", True):
                        msg = (
                            f"⚠️ *[{acc_name}] Достигнут 100% лимит Claude Code!*\n\n"
                            f"⏳ Сброс ожидается через: *{format_countdown(reset5_dt)}* (в {format_local_time(reset5_dt)})\n\n"
                            "🔔 Я пришлю уведомление, как только лимит сбросится!"
                        )
                        send_telegram_message(bot_token, chat_id, msg)
                        account_states[acc_id] = True

                    # Переход в состояние "Лимит Сбросился"
                    elif not is_limited and was_limited and config.get("notify_on_reset", True):
                        msg = (
                            f"🎉 *[{acc_name}] Лимиты Claude Code сбросились!*\n\n"
                            f"🟢 5-часовой лимит доступен (использовано: {p5:.1f}%).\n"
                            "Вы можете продолжать работу!"
                        )
                        send_telegram_message(bot_token, chat_id, msg)
                        account_states[acc_id] = False

                    else:
                        account_states[acc_id] = is_limited

                config["account_states"] = account_states
                save_config(config)

            except Exception as e:
                print(f"⚠️ Ошибка проверки лимитов: {e}")

        # 2. Обработка команд из Telegram бота
        try:
            updates = telegram_get_updates(bot_token, offset)
            if updates and updates.get("ok") and updates.get("result"):
                for up in updates["result"]:
                    offset = up["update_id"] + 1
                    if "message" in up and "text" in up["message"]:
                        text = up["message"]["text"].strip().lower()
                        msg_chat_id = str(up["message"]["chat"]["id"])

                        if text in ["/status", "/check", "статус", "лимиты"]:
                            try:
                                results = fetch_all_accounts_usage(config)
                                report = format_status_report(results)
                                send_telegram_message(bot_token, msg_chat_id, report)
                            except Exception as err:
                                send_telegram_message(bot_token, msg_chat_id, f"❌ Ошибка получения лимитов: {err}")

                        elif text in ["/start", "/help", "помощь"]:
                            help_text = (
                                "🤖 *Claude Code Limits Checker (Coolify / Docker)*\n\n"
                                "Команды:\n"
                                "• `/status` или `/check` — проверить текущие лимиты всех аккаунтов\n"
                                "• `/help` — справка\n\n"
                                "Бот автоматически проверяет ваши аккаунты и уведомляет, когда лимиты превышены или сбросились!"
                            )
                            send_telegram_message(bot_token, msg_chat_id, help_text)
        except Exception as e:
            print(f"⚠️ Ошибка обработки сообщений Telegram: {e}")

        time.sleep(3)

# --- CLI Entrypoint ---

def main():
    parser = argparse.ArgumentParser(description="Claude Code Limits Checker (Coolify / Docker Support)")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("status", help="Показать текущий статус всех аккаунтов в консоли")
    subparsers.add_parser("send", help="Отправить текущий отчет по всем аккаунтам в Telegram")
    subparsers.add_parser("setup", help="Интерактивная настройка Telegram бота")
    subparsers.add_parser("accounts", help="Управление аккаунтами")
    subparsers.add_parser("export", help="Сформировать config.json с токенами для Coolify / Docker")
    subparsers.add_parser("daemon", help="Запустить фоновый монитор и Telegram бот")

    args = parser.parse_args()
    config = load_config()

    if args.command == "status" or args.command is None:
        results = fetch_all_accounts_usage(config)
        print(format_console_report(results))

    elif args.command == "send":
        try:
            results = fetch_all_accounts_usage(config)
            report = format_status_report(results)
            if send_telegram_message(config.get("bot_token"), config.get("chat_id"), report):
                print("✅ Отчет успешно отправлен в Telegram!")
            else:
                print("❌ Не удалось отправить отчет в Telegram.")
        except Exception as e:
            print(f"❌ Ошибка: {e}")

    elif args.command == "setup":
        run_setup()

    elif args.command == "accounts":
        manage_accounts()

    elif args.command == "export":
        exported = export_config()
        valid_accs = exported.get("accounts", [])

        print(f"\n📋 СФОРМИРОВАННЫЙ ЭКСПОРТ (Найдено аккаунтов с токенами: {len(valid_accs)})")
        print("=" * 60)

        if not valid_accs:
            print("⚠️ ВНИМАНИЕ: Не найдено ни одного аккаунта с действующими токенами!")
            print("Чтобы добавить аккаунты с токенами:")
            print("  1. Выполните 'claude auth login' для входа под нужным аккаунтом на Mac.")
            print("  2. Или выполните 'python3 claude_checker.py accounts' и добавьте токены вручную.")
        else:
            for idx, a in enumerate(valid_accs, 1):
                print(f"  {idx}. {a.get('name')} (Access Token: {'OK' if a.get('access_token') else 'Нет'}, Refresh Token: {'OK' if a.get('refresh_token') else 'Нет'})")

        print("\n📋 ПЕРЕМЕННАЯ ОКРУЖЕНИЯ ДЛЯ COOLIFY (CONFIG_JSON):")
        print("=" * 60)
        json_compact = json.dumps(exported, ensure_ascii=False)
        print(f"CONFIG_JSON='{json_compact}'")
        print("=" * 60)

        print("\n📋 ИЛИ ФАЙЛ CONFIG.JSON ДЛЯ ВАШЕГО DOCKER VOLUME:")
        print("=" * 60)
        json_pretty = json.dumps(exported, ensure_ascii=False, indent=2)
        print(json_pretty)
        print("=" * 60)

        export_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.export.json")
        try:
            with open(export_file, "w", encoding="utf-8") as f:
                f.write(json_pretty)
            print(f"✅ Экспорт также сохранен в файл: {export_file}")
        except Exception as e:
            print(f"⚠️ Ошибка записи файла экспорта: {e}")

    elif args.command == "daemon":
        run_daemon()

if __name__ == "__main__":
    main()
