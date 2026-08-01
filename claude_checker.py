#!/usr/bin/env python3
"""
Claude Code Limits Checker & Telegram Notifier
----------------------------------------------
Multi-Account & Remote Server / Coolify / Docker Support.
Часовой пояс: UTC+2. Детальное логирование и удаление конфликтных Webhook.
"""

import os
import sys
import json
import time
import re
import html
import platform
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import argparse
import threading
from datetime import datetime, timezone, timedelta

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# Изменяемое состояние (ротируемые токены, флаги лимитов) живёт ОТДЕЛЬНО от config.json,
# потому что в Coolify/Docker конфиг приходит из ENV и перекрывает файл при каждом старте.
# Путь переопределяется через ENV STATE_FILE — в контейнере укажите его на volume.
STATE_FILE = os.environ.get("STATE_FILE") or os.path.join(BASE_DIR, "state.json")

DEFAULT_KEYCHAIN_SERVICE = "Claude Code-credentials"
API_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_REFRESH_URLS = [
    "https://console.anthropic.com/v1/oauth/token",
    "https://platform.claude.com/v1/oauth/token",
]
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USER_AGENT = "claude-code/0.2.29"

IS_MACOS = platform.system() == "Darwin"

# Часовой пояс UTC+2
TZ_OFFSET_HOURS = 2
DISPLAY_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

def escape_html(text):
    if not text:
        return ""
    return html.escape(str(text))

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

    # 1. Загрузка из config.json только если это реальный ФАЙЛ
    if os.path.exists(CONFIG_FILE) and os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                file_cfg = json.load(f)
                cfg.update(file_cfg)
        except (IsADirectoryError, OSError, json.JSONDecodeError):
            pass

    # 2. Переопределение / загрузка из ENV переменных (для Coolify / Docker)
    env_config_json = os.environ.get("CONFIG_JSON")
    if env_config_json:
        try:
            raw_val = env_config_json.strip()
            if (raw_val.startswith("'") and raw_val.endswith("'")) or (raw_val.startswith('"') and raw_val.endswith('"')):
                raw_val = raw_val[1:-1].strip()
            parsed = json.loads(raw_val)
            # accounts сливаем по ключу, а не затираем: иначе аккаунты из файла пропадают
            parsed_accounts = parsed.pop("accounts", None)
            cfg.update(parsed)
            if parsed_accounts is not None:
                cfg["accounts"] = merge_accounts(cfg.get("accounts", []), parsed_accounts)
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
            cfg["accounts"] = merge_accounts(cfg.get("accounts", []), json.loads(env_accounts))
        except Exception as e:
            print(f"⚠️ Ошибка разбора ACCOUNTS_JSON из ENV: {e}")

    if "accounts" not in cfg:
        cfg["accounts"] = []

    return cfg

def account_key(account):
    """Стабильный идентификатор аккаунта для state-файла и merge."""
    return str(
        account.get("id")
        or account.get("keychain_service")
        or account.get("name")
        or ""
    )

def merge_accounts(base, incoming):
    """Сливает списки аккаунтов по account_key; incoming имеет приоритет по полям."""
    merged = []
    by_key = {}
    for acc in list(base) + list(incoming):
        key = account_key(acc)
        if key and key in by_key:
            by_key[key].update(acc)
        else:
            copy = dict(acc)
            merged.append(copy)
            if key:
                by_key[key] = copy
    return merged

def save_config(config):
    if os.path.exists(CONFIG_FILE) and os.path.isdir(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# --- Persistent State (ротируемые токены + флаги лимитов) ---

_state_lock = threading.Lock()

def load_state():
    if os.path.exists(STATE_FILE) and os.path.isfile(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("tokens", {})
                    data.setdefault("account_states", {})
                    return data
        except (IsADirectoryError, OSError, json.JSONDecodeError) as e:
            print(f"⚠️ Не удалось прочитать state-файл {STATE_FILE}: {e}")
    return {"tokens": {}, "account_states": {}}

def save_state(state):
    """Атомарная запись: обрыв на середине не должен убить refresh-токены."""
    if os.path.exists(STATE_FILE) and os.path.isdir(STATE_FILE):
        print(f"⚠️ STATE_FILE указывает на директорию: {STATE_FILE}")
        return False
    tmp_path = f"{STATE_FILE}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_FILE)
        return True
    except Exception as e:
        print(f"⚠️ Не удалось сохранить state-файл {STATE_FILE}: {e}")
        return False

def store_tokens(acc_id, access_token, refresh_token, source_refresh=None):
    """Сохраняет ротированную пару токенов. Без этого аккаунт умирает при рестарте.

    source_refresh — токен из конфига, от которого пошла цепочка ротаций.
    По нему определяем, что администратор залил свежий экспорт, и state устарел.
    """
    if not acc_id:
        return
    with _state_lock:
        state = load_state()
        entry = state.setdefault("tokens", {}).get(acc_id) or {}
        entry.update({
            "access_token": access_token or "",
            "refresh_token": refresh_token or "",
            "updated_at": datetime.now(timezone.utc).isoformat()
        })
        if source_refresh:
            entry["source_refresh"] = source_refresh
        state["tokens"][acc_id] = entry
        ok = save_state(state)
    if not ok:
        print(f"❌ ВНИМАНИЕ: новый refresh-токен для '{acc_id}' не сохранён. "
              f"После рестарта аккаунт потребует повторного экспорта.")

def load_stored_tokens(acc_id, config_refresh=None):
    """Возвращает сохранённую пару токенов.

    Если в конфиге лежит другой исходный refresh-токен, чем тот, от которого
    накопилось состояние, значит конфиг переэкспортировали — state игнорируем,
    иначе свежий токен так и остался бы затенён мёртвым.
    """
    if not acc_id:
        return None, None
    entry = load_state().get("tokens", {}).get(acc_id) or {}
    if not entry:
        return None, None

    source = entry.get("source_refresh")
    if config_refresh and source and config_refresh != source:
        print(f"ℹ️ Обнаружен новый экспорт для '{acc_id}' — использую токены из конфига.")
        return None, None

    return entry.get("access_token") or None, entry.get("refresh_token") or None

def get_primary_email():
    p = os.path.expanduser("~/.claude.json")
    if os.path.exists(p) and os.path.isfile(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
                return d.get("oauthAccount", {}).get("emailAddress")
        except Exception:
            pass
    return None

# --- Keychain (macOS) ---

def discover_keychain_entries():
    """Возвращает список (service, account).

    Claude Code CLI пишет ровно одну запись: service='Claude Code-credentials',
    acct=<имя пользователя macOS>. Аккаунты в ней НЕ разделяются — перелогин
    перезаписывает запись. Поэтому несколько аккаунтов Keychain-ом не отследить;
    второй и последующие нужно заводить как type='token' (см. команду export).
    """
    if not IS_MACOS:
        return []

    entries = []
    try:
        res = subprocess.run(["security", "dump-keychain"], capture_output=True, text=True, errors="ignore")
        # acct и svce идут в одном блоке атрибутов; разбираем поэлементно
        for block in res.stdout.split("keychain: "):
            svce = re.search(r'"svce"<blob>="([^"]*Claude Code-credentials[^"]*)"', block)
            if not svce:
                continue
            acct = re.search(r'"acct"<blob>="([^"]*)"', block)
            entries.append((svce.group(1), acct.group(1) if acct else None))
        entries = sorted(set(entries))
    except Exception:
        pass

    if not entries and is_keychain_service_exists(DEFAULT_KEYCHAIN_SERVICE):
        entries = [(DEFAULT_KEYCHAIN_SERVICE, None)]
    return entries

def discover_keychain_services():
    return sorted({svc for svc, _acct in discover_keychain_entries()})

def is_keychain_service_exists(service_name):
    if not IS_MACOS:
        return False
    try:
        res = subprocess.run(["security", "find-generic-password", "-s", service_name], capture_output=True, text=True)
        return res.returncode == 0
    except Exception:
        return False

def _keychain_args(service_name, account_name):
    args = ["-s", service_name]
    if account_name:
        args += ["-a", account_name]
    return args

def get_keychain_credentials(service_name=DEFAULT_KEYCHAIN_SERVICE, account_name=None):
    if not IS_MACOS:
        raise RuntimeError("Keychain доступен только на macOS")
    try:
        res = subprocess.run(
            ["security", "find-generic-password"] + _keychain_args(service_name, account_name) + ["-w"],
            capture_output=True, text=True, check=True
        )
        data = json.loads(res.stdout.strip())
        oauth = data.get("claudeAiOauth", {})
        return oauth.get("accessToken", ""), oauth.get("refreshToken", ""), oauth.get("scopes", [])
    except Exception as e:
        raise RuntimeError(f"Не удалось прочитать Keychain ({service_name}): {e}")

def update_keychain_credentials(service_name, new_access_token, new_refresh_token, account_name=None):
    """Обновляет запись строго на месте.

    -a обязателен при записи: без него add-generic-password -U может попасть
    не в ту запись, если в связке есть несколько элементов с этим service.
    """
    if not IS_MACOS:
        return
    try:
        res = subprocess.run(
            ["security", "find-generic-password"] + _keychain_args(service_name, account_name) + ["-w"],
            capture_output=True, text=True, check=True
        )
        data = json.loads(res.stdout.strip())
        if "claudeAiOauth" not in data:
            return

        if not account_name:
            # определяем acct существующей записи, чтобы писать точечно
            info = subprocess.run(
                ["security", "find-generic-password", "-s", service_name],
                capture_output=True, text=True
            )
            m = re.search(r'"acct"<blob>="([^"]*)"', info.stdout)
            account_name = m.group(1) if m else None

        data["claudeAiOauth"]["accessToken"] = new_access_token
        data["claudeAiOauth"]["refreshToken"] = new_refresh_token
        subprocess.run(
            ["security", "add-generic-password", "-U"]
            + _keychain_args(service_name, account_name)
            + ["-w", json.dumps(data)],
            capture_output=True, check=True
        )
    except Exception as e:
        print(f"⚠️ Не удалось обновить Keychain ({service_name}): {e}")

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
    last_err = None

    for url in TOKEN_REFRESH_URLS:
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT
            }
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                res_json = json.loads(resp.read().decode("utf-8"))
                new_access_token = res_json.get("access_token")
                if not new_access_token:
                    raise RuntimeError("Ответ OAuth не содержит access_token")
                new_refresh_token = res_json.get("refresh_token") or refresh_token
                return new_access_token, new_refresh_token
        except urllib.error.HTTPError as e:
            err_msg = ""
            raw_body = ""
            try:
                raw_body = e.read().decode("utf-8", errors="replace").strip()
                err_body = json.loads(raw_body)
                err_msg = err_body.get("error", {}).get("message") or err_body.get("error") or str(err_body)
            except Exception:
                # тело не JSON — показываем как есть, иначе причина 400 остаётся невидимой
                err_msg = raw_body[:300]

            # invalid_grant — токен мёртв, другой эндпоинт не поможет
            if "invalid_grant" in str(err_msg).lower():
                raise RuntimeError("Сессия истекла (invalid_grant). Требуется повторный вход в аккаунт.")
            # Эндпоинт мог переехать: 404/405, а равно и невнятный 400 без
            # диагностики — повод попробовать следующий адрес, а не сдаваться.
            if e.code in (400, 404, 405):
                last_err = RuntimeError(f"HTTP {e.code} на {url}: {err_msg or 'без деталей'}")
                continue
            raise RuntimeError(f"HTTP {e.code}: {err_msg or e.reason}")
        except urllib.error.URLError as e:
            last_err = RuntimeError(f"Сеть недоступна при обращении к {url}: {e.reason}")
            continue
        except (TimeoutError, json.JSONDecodeError) as e:
            last_err = RuntimeError(f"Некорректный ответ от {url}: {e}")
            continue

    raise last_err or RuntimeError("Не удалось обновить OAuth-токен")

# --- Account Usage Fetcher ---

def get_active_accounts(config):
    configured = config.get("accounts", [])
    if not IS_MACOS or not config.get("auto_discover_keychain", True):
        return configured

    discovered = discover_keychain_entries()
    accounts = list(configured)
    existing_services = {a.get("keychain_service") for a in configured if a.get("type") == "keychain"}
    primary_email = get_primary_email()

    for svc, acct in discovered:
        if svc not in existing_services:
            if svc == DEFAULT_KEYCHAIN_SERVICE:
                label = primary_email if primary_email else "Основной аккаунт"
            else:
                label = f"Аккаунт ({svc.replace('Claude Code-credentials-', '')})"

            accounts.append({
                "id": svc,
                "name": label,
                "type": "keychain",
                "keychain_service": svc,
                "keychain_account": acct
            })

    if not accounts and IS_MACOS:
        accounts.append({
            "id": DEFAULT_KEYCHAIN_SERVICE,
            "name": primary_email if primary_email else "Основной аккаунт",
            "type": "keychain",
            "keychain_service": DEFAULT_KEYCHAIN_SERVICE
        })

    return accounts

def fetch_usage_for_token(token, retries=2):
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json"
    }

    for attempt in range(retries + 1):
        req = urllib.request.Request(API_USAGE_URL, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                retry_after_str = e.headers.get("retry-after") or e.headers.get("Retry-After")
                try:
                    wait_sec = float(retry_after_str) if retry_after_str else 3.0
                except ValueError:
                    wait_sec = 3.0

                time.sleep(wait_sec)
                continue
            raise e

def fetch_account_usage(account, config):
    acc_type = account.get("type", "keychain" if IS_MACOS else "token")

    try:
        if acc_type == "keychain" and IS_MACOS:
            svc = account.get("keychain_service", DEFAULT_KEYCHAIN_SERVICE)
            acct = account.get("keychain_account")
            token, rf_token, _ = get_keychain_credentials(svc, acct)

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
                            update_keychain_credentials(svc, new_acc, new_rf, acct)
                            data = fetch_usage_for_token(new_acc)
                            return data, None
                        except Exception as refresh_err:
                            print(f"⚠️ Обновление токена Keychain ({svc}) не удалось: {refresh_err}")
                    if svc == DEFAULT_KEYCHAIN_SERVICE:
                        token = refresh_claude_cli_token()
                        if token:
                            data = fetch_usage_for_token(token)
                            return data, None
                raise e

        elif acc_type == "token":
            acc_id = account_key(account)
            config_refresh = account.get("refresh_token")

            # Токены из state-файла новее конфига: OAuth ротирует refresh-токен
            # при каждом обновлении, а config.json/CONFIG_JSON остаются со старым.
            stored_access, stored_refresh = load_stored_tokens(acc_id, config_refresh)
            token = stored_access or account.get("access_token")
            rf_token = stored_refresh or config_refresh

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
                except Exception as refresh_err:
                    # Если протух токен из state — пробуем исходный из конфига один раз
                    if stored_refresh and config_refresh and config_refresh != rf_token:
                        try:
                            new_acc, new_rf = refresh_oauth_token_direct(config_refresh)
                        except Exception as e2:
                            return None, f"{e2}"
                    else:
                        return None, f"{refresh_err}"

                account["access_token"] = new_acc
                store_tokens(acc_id, new_acc, new_rf, source_refresh=config_refresh)

                data = fetch_usage_for_token(new_acc)
                return data, None

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
    """Опрашивает аккаунты параллельно.

    Раньше здесь была последовательная пауза 2 с на аккаунт — вместе с таймаутами
    и ретраями это блокировало ответ бота на десятки секунд. Аккаунты независимы,
    поэтому идут одновременно; от 429 защищает retry-логика в fetch_usage_for_token.
    """
    accounts = get_active_accounts(config)
    if not accounts:
        return []

    results = [None] * len(accounts)

    def worker(idx, acc):
        usage, err = fetch_account_usage(acc, config)
        results[idx] = {"account": acc, "usage": usage, "error": err}

    threads = []
    for idx, acc in enumerate(accounts):
        t = threading.Thread(target=worker, args=(idx, acc), daemon=True)
        t.start()
        threads.append(t)

    for t in threads:
        t.join(timeout=60)

    return [
        r if r is not None else {
            "account": accounts[i],
            "usage": None,
            "error": "Таймаут запроса к API (60 с)"
        }
        for i, r in enumerate(results)
    ]

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
                        label = primary_email if primary_email else "Основной аккаунт"
                    else:
                        label = f"Аккаунт ({svc.replace('Claude Code-credentials-', '')})"

                    export_accounts.append({
                        # стабильный id нужен state-файлу, чтобы ротированные
                        # токены не терялись при переименовании аккаунта
                        "id": svc,
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
                # если для аккаунта уже есть ротированные токены — экспортируем их,
                # иначе на сервер уедет заведомо протухшая пара
                st_access, st_refresh = load_stored_tokens(account_key(acc))
                export_accounts.append({
                    "id": account_key(acc) or f"token_{len(export_accounts)}",
                    "name": acc.get("name", "Доп. аккаунт"),
                    "type": "token",
                    "access_token": st_access or acc_token,
                    "refresh_token": st_refresh or rf_token,
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

# --- Output Formatters (HTML) ---

def format_status_report(results):
    if not results:
        return "⚠️ Нет настроенных аккаунтов для проверки."

    lines = [f"📊 <b>Состояние лимитов Claude Code</b> ({len(results)} акк.)\n"]

    for idx, item in enumerate(results, 1):
        acc = item["account"]
        acc_name = escape_html(acc.get("name") or f"Аккаунт #{idx}")
        data = item["usage"]
        err = item["error"]

        lines.append(f"👤 <b>{acc_name}</b>")

        if err:
            lines.append(f"   ⚠️ <i>Ошибка: {escape_html(err)}</i>\n")
            continue

        five_h = data.get("five_hour", {})
        p5 = five_h.get("utilization", 0.0) or 0.0
        reset5_dt = parse_iso_time(five_h.get("resets_at"))
        emoji5 = get_status_emoji(p5)

        lines.append(f"   {emoji5} <b>5-часовой лимит:</b> <code>{p5:.1f}%</code>")
        if p5 >= 100:
            lines.append(f"      • ⏳ Сброс через: <b>{format_countdown(reset5_dt)}</b> (в {format_local_time(reset5_dt)})")
        elif reset5_dt and (reset5_dt - datetime.now(timezone.utc)).total_seconds() > 0:
            lines.append(f"      • ⏳ Сброс в: {format_local_time(reset5_dt)} (через {format_countdown(reset5_dt)})")

        seven_d = data.get("seven_day", {})
        p7 = seven_d.get("utilization", 0.0) or 0.0
        reset7_dt = parse_iso_time(seven_d.get("resets_at"))
        emoji7 = get_status_emoji(p7)

        lines.append(f"   {emoji7} <b>7-дневный лимит:</b> <code>{p7:.1f}%</code>")
        if reset7_dt and (reset7_dt - datetime.now(timezone.utc)).total_seconds() > 0:
            lines.append(f"      • ⏳ Сброс в: {format_local_time(reset7_dt)} (через {format_countdown(reset7_dt)})")

        extra = data.get("extra_usage") or {}
        if extra.get("is_enabled"):
            p_ext = extra.get("utilization", 0.0) or 0.0
            used_c = extra.get("used_credits", 0.0) or 0.0
            limit_c = extra.get("monthly_limit", 0.0) or 0.0
            emoji_ext = get_status_emoji(p_ext)
            lines.append(f"   {emoji_ext} <b>Extra Usage:</b> <code>{p_ext:.1f}%</code> (${used_c / 100:.2f} / ${limit_c / 100:.2f})")

        lines.append("")

    now_str = datetime.now(DISPLAY_TZ).strftime("%d.%m.%Y %H:%M:%S (UTC+2)")
    lines.append(f"<i>Обновлено: {now_str}</i>")

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
        elif reset5_dt:
            lines.append(f"    --> Сброс в: {format_local_time(reset5_dt)} (через {format_countdown(reset5_dt)})")

        seven_d = data.get("seven_day", {})
        p7 = seven_d.get("utilization", 0.0) or 0.0
        reset7_dt = parse_iso_time(seven_d.get("resets_at"))

        lines.append(f"  • 7-дневный лимит: {p7:.1f}%")
        if reset7_dt:
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

def delete_telegram_webhook(bot_token):
    if not bot_token:
        return
    url = f"https://api.telegram.org/bot{bot_token}/deleteWebhook?drop_pending_updates=False"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
    except Exception:
        pass

def send_chat_action(bot_token, chat_id, action="typing"):
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendChatAction"
    payload = {"chat_id": chat_id, "action": action}
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            pass
    except Exception:
        pass

def strip_html_markup(text):
    clean = re.sub(r"</?(b|i|code|pre|u|s|a)(\s[^>]*)?>", "", text)
    return (clean.replace("&lt;", "<")
                 .replace("&gt;", ">")
                 .replace("&quot;", '"')
                 .replace("&#x27;", "'")
                 .replace("&amp;", "&"))

def telegram_call(bot_token, method, payload, parse_mode="HTML", timeout=10):
    """Общий вызов Bot API. Возвращает поле result или None.

    На 400 один раз повторяет запрос без HTML-разметки: это единственный код,
    при котором виновата разметка. На 429/5xx повтор только вредит.
    """
    if not bot_token or not payload.get("chat_id"):
        print("⚠️ Bot token или Chat ID не настроены!")
        return None

    body = dict(payload)
    if parse_mode and "text" in body:
        body["parse_mode"] = parse_mode

    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res_json = json.loads(resp.read().decode("utf-8"))
            return res_json.get("result") if res_json.get("ok") else None
    except urllib.error.HTTPError as e:
        if e.code == 400 and parse_mode is not None and "text" in payload:
            retry = dict(payload)
            retry["text"] = strip_html_markup(payload["text"])
            return telegram_call(bot_token, method, retry, parse_mode=None, timeout=timeout)
        print(f"⚠️ Ошибка Telegram {method} (HTTP {e.code}): {e}")
        return None
    except Exception as e:
        print(f"⚠️ Ошибка Telegram {method}: {e}")
        return None

def send_telegram_message(bot_token, chat_id, text, parse_mode="HTML"):
    return send_telegram_message_id(bot_token, chat_id, text, parse_mode) is not None

def send_telegram_message_id(bot_token, chat_id, text, parse_mode="HTML"):
    """Как send_telegram_message, но возвращает message_id — нужен для редактирования."""
    result = telegram_call(bot_token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True
    }, parse_mode)
    return result.get("message_id") if isinstance(result, dict) else None

def edit_telegram_message(bot_token, chat_id, message_id, text, parse_mode="HTML"):
    if not message_id:
        return False
    result = telegram_call(bot_token, "editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True
    }, parse_mode)
    return result is not None

def telegram_get_updates(bot_token, offset=None, long_poll=25):
    """Long polling: Telegram держит соединение до появления апдейта.

    Ответ приходит мгновенно, а не через фиксированный sleep, как раньше.
    HTTP-таймаут всегда больше серверного, иначе рвём соединение сами.
    """
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates?timeout={long_poll}"
    if offset:
        url += f"&offset={offset}"
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=long_poll + 10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"⚠️ Ошибка getUpdates в Telegram: {e}")
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

    delete_telegram_webhook(config["bot_token"])

    offset = None
    chat_id = None
    start_time = time.time()

    while time.time() - start_time < 60:
        updates = telegram_get_updates(config["bot_token"], offset, long_poll=5)
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
        if send_telegram_message(config["bot_token"], config["chat_id"], "🤖 <b>Claude Limits Checker</b> успешно настроен и готов к работе!"):
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

HELP_TEXT = (
    "🤖 <b>Claude Code Limits Checker (Coolify / Docker)</b>\n\n"
    "Команды:\n"
    "• <code>/status</code> или <code>/check</code> — проверить текущие лимиты всех аккаунтов\n"
    "• <code>/help</code> — справка\n\n"
    "Бот автоматически проверяет ваши аккаунты и уведомляет, когда лимиты превышены или сбросились!"
)

def parse_command(raw_text):
    """Разбирает первое слово как команду. Учитывает формат /status@MyBot.

    Раньше использовалась проверка вида "статус" in text — она срабатывала
    на подстроку в любом сообщении.
    """
    if not raw_text:
        return None
    first = raw_text.strip().split()[0].lower()
    if first.startswith("/"):
        first = first.split("@", 1)[0]
        return first
    if first in ("статус", "лимиты", "лимит"):
        return "/status"
    if first in ("помощь", "справка", "старт"):
        return "/help"
    return None

def allowed_chat_ids(config):
    """Белый список чатов. Без него бот отвечает кому угодно, кто его нашёл."""
    ids = set()
    primary = config.get("chat_id")
    if primary:
        ids.add(str(primary).strip())
    extra = config.get("allowed_chat_ids") or os.environ.get("ALLOWED_CHAT_IDS", "")
    if isinstance(extra, str):
        extra = [x for x in re.split(r"[,\s]+", extra) if x]
    for x in extra or []:
        ids.add(str(x).strip())
    return ids

def monitor_loop(config, bot_token, chat_id, stop_event):
    """Периодическая проверка лимитов в отдельном потоке.

    Вынесено из главного цикла: раньше долгая проверка (таймауты, ретраи 429,
    refresh) блокировала чтение команд, и бот молчал десятки секунд.
    """
    check_interval = max(60, config.get("check_interval_minutes", 5) * 60)

    with _state_lock:
        account_states = dict(load_state().get("account_states", {}))

    # Даём боту принять команды до первой тяжёлой проверки
    if stop_event.wait(5):
        return

    while not stop_event.is_set():
        try:
            print("🔄 Периодическая автопроверка лимитов всех аккаунтов...")
            results = fetch_all_accounts_usage(config)

            for item in results:
                acc = item["account"]
                acc_id = account_key(acc)
                acc_name = escape_html(acc.get("name", "Аккаунт"))
                data = item["usage"]

                if not data:
                    if item.get("error"):
                        print(f"⚠️ [{acc.get('name')}] {item['error']}")
                    continue

                five_h = data.get("five_hour", {})
                p5 = five_h.get("utilization", 0.0) or 0.0
                reset5_dt = parse_iso_time(five_h.get("resets_at"))

                was_limited = account_states.get(acc_id, False)
                is_limited = p5 >= 100.0

                if is_limited and not was_limited and config.get("notify_on_limit_reached", True):
                    send_telegram_message(bot_token, chat_id, (
                        f"⚠️ <b>[{acc_name}] Достигнут 100% лимит Claude Code!</b>\n\n"
                        f"⏳ Сброс ожидается через: <b>{format_countdown(reset5_dt)}</b> (в {format_local_time(reset5_dt)})\n\n"
                        "🔔 Я пришлю уведомление, как только лимит сбросится!"
                    ))
                elif not is_limited and was_limited and config.get("notify_on_reset", True):
                    send_telegram_message(bot_token, chat_id, (
                        f"🎉 <b>[{acc_name}] Лимиты Claude Code сбросились!</b>\n\n"
                        f"🟢 5-часовой лимит доступен (использовано: <code>{p5:.1f}%</code>).\n"
                        "Вы можете продолжать работу!"
                    ))

                account_states[acc_id] = is_limited

            # Состояние в state-файле, а не в config.json: иначе в ENV-режиме
            # оно теряется при рестарте и уведомления дублируются.
            with _state_lock:
                state = load_state()
                state["account_states"] = account_states
                save_state(state)

        except Exception as e:
            print(f"⚠️ Ошибка проверки лимитов: {e}")

        stop_event.wait(check_interval)

# Индикатор "печатает" в Telegram гаснет через ~5 с, поэтому его нужно обновлять.
TYPING_REFRESH_SEC = 4
# Пороги, на которых пользователю сообщается, что запрос всё ещё выполняется.
PROGRESS_STEPS_SEC = (6, 15, 30)

def waiting_indicator(bot_token, chat_id, message_id, stop_event, started_at):
    """Держит "печатает" живым и обновляет сообщение-заглушку, если ответ затянулся.

    Без этого при медленном ответе Anthropic (или ретраях на 429) пользователь
    видит тишину и не понимает, принял бот команду или нет.
    """
    remaining = list(PROGRESS_STEPS_SEC)
    next_typing = started_at

    while not stop_event.is_set():
        now = time.time()

        if now >= next_typing:
            send_chat_action(bot_token, chat_id, "typing")
            next_typing = now + TYPING_REFRESH_SEC

        elapsed = now - started_at
        if remaining and elapsed >= remaining[0]:
            threshold = remaining.pop(0)
            edit_telegram_message(
                bot_token, chat_id, message_id,
                f"⏳ <i>Опрашиваю аккаунты… ({int(threshold)}+ с)</i>\n"
                f"<i>Anthropic отвечает медленнее обычного, жду.</i>"
            )
            continue

        # Просыпаемся ровно к ближайшему событию, а не по фиксированному тику:
        # иначе порог в 6 с срабатывал бы только на 8-й секунде.
        wake_at = next_typing
        if remaining:
            wake_at = min(wake_at, started_at + remaining[0])
        if stop_event.wait(max(0.05, wake_at - time.time())):
            return

def handle_command(config, bot_token, cmd, msg_chat_id):
    if cmd in ("/status", "/check"):
        started_at = time.time()
        send_chat_action(bot_token, msg_chat_id, "typing")

        # Заглушка отправляется сразу: подтверждает приём команды за доли секунды,
        # а в конце заменяется готовым отчётом — вместо второго сообщения.
        placeholder_id = send_telegram_message_id(
            bot_token, msg_chat_id, "⏳ <i>Проверяю лимиты аккаунтов…</i>"
        )

        stop_event = threading.Event()
        indicator = threading.Thread(
            target=waiting_indicator,
            args=(bot_token, msg_chat_id, placeholder_id, stop_event, started_at),
            daemon=True
        )
        indicator.start()

        try:
            results = fetch_all_accounts_usage(config)
            text = format_status_report(results)
        except Exception as err:
            print(f"❌ Ошибка получения лимитов для {cmd}: {err}")
            text = f"❌ <i>Ошибка получения лимитов: {escape_html(err)}</i>"
        finally:
            # Останавливаем индикатор до финальной записи, иначе он может
            # затереть готовый отчёт своим последним обновлением.
            stop_event.set()
            indicator.join(timeout=5)

        elapsed = time.time() - started_at
        sent = edit_telegram_message(bot_token, msg_chat_id, placeholder_id, text)
        if not sent:
            sent = send_telegram_message(bot_token, msg_chat_id, text)

        print(f"📤 [Telegram] Ответ на {cmd} за {elapsed:.1f} с: {'Успешно' if sent else 'Ошибка'}")

    elif cmd in ("/start", "/help"):
        send_telegram_message(bot_token, msg_chat_id, HELP_TEXT)

def run_daemon():
    config = load_config()
    bot_token = config.get("bot_token")
    chat_id = config.get("chat_id")

    if not bot_token or not chat_id:
        print("❌ Ошибка: Скрипт не настроен! Задайте переменные окружения BOT_TOKEN и CHAT_ID в Coolify или запустите: python3 claude_checker.py setup")
        sys.exit(1)

    allowed = allowed_chat_ids(config)

    print("🚀 Запуск фонового демона мониторинга (Multi-Account & Coolify) и Telegram-бота...")
    print(f"   Bot Token: {'Настроен' if bot_token else 'ОТСУТСТВУЕТ'}")
    print(f"   Chat ID: {chat_id if chat_id else 'ОТСУТСТВУЕТ'}")
    print(f"   Разрешённые чаты: {', '.join(sorted(allowed))}")
    print(f"   Загружено аккаунтов: {len(get_active_accounts(config))}")
    print(f"   State-файл: {STATE_FILE}")
    print(f"   Частота проверки: каждые {config.get('check_interval_minutes', 5)} мин. Часовой пояс: UTC+2.")

    # Удаляем старые вебхуки для работы через getUpdates
    delete_telegram_webhook(bot_token)

    send_telegram_message(
        bot_token, chat_id,
        "🚀 <b>Claude Limits Checker запущен в контейнере!</b>\n"
        "Отправьте <code>/status</code> или <code>/check</code> для получения текущего состояния всех аккаунтов."
    )

    stop_event = threading.Event()
    monitor = threading.Thread(
        target=monitor_loop,
        args=(config, bot_token, chat_id, stop_event),
        daemon=True
    )
    monitor.start()

    offset = None
    try:
        while True:
            try:
                updates = telegram_get_updates(bot_token, offset)
                if not (updates and updates.get("ok")):
                    # Ошибка сети/API — короткая пауза, чтобы не долбить в цикле
                    time.sleep(3)
                    continue

                for up in updates.get("result", []):
                    offset = up["update_id"] + 1
                    msg = up.get("message") or up.get("edited_message")
                    if not msg or "text" not in msg:
                        continue

                    raw_text = msg["text"].strip()
                    msg_chat_id = str(msg["chat"]["id"])

                    if msg_chat_id not in allowed:
                        print(f"🚫 [Telegram] Игнорирую сообщение от постороннего chat_id={msg_chat_id}")
                        continue

                    print(f"📩 [Telegram] Получено сообщение от chat_id={msg_chat_id}: '{raw_text}'")

                    cmd = parse_command(raw_text)
                    if cmd:
                        # Каждая команда в своём потоке: долгий /status не блокирует
                        # приём следующих сообщений
                        threading.Thread(
                            target=handle_command,
                            args=(config, bot_token, cmd, msg_chat_id),
                            daemon=True
                        ).start()

            except Exception as e:
                print(f"⚠️ Ошибка обработки сообщений Telegram: {e}")
                time.sleep(3)
    except KeyboardInterrupt:
        print("\n👋 Остановка демона...")
        stop_event.set()

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
