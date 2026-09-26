# Многопользовательский бот Claude Limits Checker — дизайн

Дата: 2026-09-26

## Цель

Перевести бота с одного пользователя (токены из Keychain/env) на закрытый многопользовательский режим:
- доступ только по инвайтам от админа;
- каждый пользователь сам добавляет свои аккаунты Claude через OAuth прямо в Telegram;
- управление через inline-меню.

## Ключевые решения

- **Владение**: аккаунт принадлежит одному пользователю; видит и получает уведомления только владелец.
- **Доступ**: только одноразовые инвайт-ссылки. Неизвестным пользователям бот не отвечает ничем.
- **Уведомления**: как сейчас («5ч лимит 100%» и «лимит сброшен») + переключатель вкл/выкл на аккаунт.
- **Стек**: Python 3.11+, aiogram 3, aiosqlite, cryptography (Fernet), aiohttp.
- **Без legacy**: `CONFIG_JSON`, `state`, Keychain, экспорт — удаляются. Старт с пустой БД. `claude_checker.py` удаляется.

## Добавление аккаунта (OAuth)

Используется ручной OAuth-флоу клиента Claude Code (как `claude setup-token`):
- `client_id` = `9d1c250a-e61b-44d9-88ed-5944d1962f5e`;
- `redirect_uri` = `https://console.anthropic.com/oauth/code/callback` — страница показывает код с кнопкой Copy.

Произвольный редирект (на `t.me`/`tg://`) невозможен: сервер принимает только зарегистрированные для `client_id` redirect_uri; localhost-редирект уходит на устройство пользователя, а не на сервер. **Первый шаг реализации — проверить это живым запросом**; если подмена сработает, флоу упрощается.

Шаги:
1. Бот генерирует `code_verifier`/`code_challenge` (S256) и `state`, сохраняет в `oauth_pending` (TTL 10 мин), шлёт кнопку-ссылку authorize.
2. Пользователь логинится, копирует код, вставляет в чат.
3. Бот принимает `code#state`, голый код или URL целиком; сверяет `state` с `tg_id`; **удаляет сообщение с кодом**.
4. Обмен кода на токены через `/v1/oauth/token`, получение email.
5. Аккаунт сохраняется сразу с названием = email (или «Аккаунт N»), показывается карточка с лимитами и кнопкой ✏️ «Переименовать».

Отмена — кнопкой или по TTL.

## Архитектура

```
app/
  main.py          точка входа: Bot + Dispatcher + фоновый монитор
  config.py        env: BOT_TOKEN, ADMIN_TELEGRAM_ID, ENCRYPTION_KEY, DB_PATH, CHECK_INTERVAL
  db.py            aiosqlite, схема, миграции по PRAGMA user_version, репозитории
  crypto.py        Fernet encrypt/decrypt
  oauth.py         PKCE, authorize URL, обмен кода, refresh
  usage.py         /api/oauth/usage, авто-refresh при 401
  monitor.py       периодическая проверка всех аккаунтов, уведомления владельцам
  bot/
    access.py      middleware доступа
    menu.py        главное меню, список и карточка аккаунта
    add_account.py FSM добавления аккаунта
    admin.py       инвайты, пользователи, отзыв, диагностика
tests/
```

## Данные (SQLite)

- `users(tg_id PK, username, role['admin'|'user'], invited_by, created_at, blocked)`
- `invites(token PK, created_by, expires_at, used_by, used_at)` — TTL по умолчанию 24 ч, одноразовые.
- `accounts(id PK, owner_tg_id, label, email, access_token_enc, refresh_token_enc, expires_at, notify_enabled, needs_relogin, created_at)`
- `account_state(account_id PK, last_5h_percent, was_limited, last_checked, last_error)`
- `oauth_pending(state PK, tg_id, code_verifier_enc, account_id NULL, expires_at)` — `account_id` задан при повторном входе в существующий аккаунт.

Админ берётся из `ADMIN_TELEGRAM_ID` и upsert-ится в `users` с ролью `admin` при каждом старте.
Токены и verifier шифруются Fernet ключом `ENCRYPTION_KEY`. Потеря ключа = все аккаунты надо переподключить (предупреждение в README).

## Доступ

- Middleware: если `tg_id` нет в `users` (или `blocked`) — апдейт игнорируется молча.
- Исключение: `/start inv_<token>` с валидным (существует, не использован, не просрочен) токеном — пользователь создаётся, инвайт помечается использованным, показывается меню. Невалидный токен — тоже молчание.

## Меню (inline)

- 📊 Статус — отчёт по всем своим аккаунтам.
- 🗂 Аккаунты — список → карточка: лимиты, 🔔 уведомления вкл/выкл, ✏️ переименовать, 🗑 удалить (с подтверждением).
- ➕ Добавить аккаунт — FSM выше.
- 👥 Доступ (только админ) — ➕ инвайт, список пользователей с «Отозвать», 🩺 диагностика.

Отзыв доступа: `blocked=1`, аккаунты пользователя и их токены удаляются.

## Монитор

Каждые `CHECK_INTERVAL` секунд: все аккаунты без `needs_relogin`, параллельно с семафором. Переходы по `account_state`:
- 5ч лимит достиг 100% и `was_limited=0` → уведомление «упёрся» (с временем сброса), `was_limited=1`;
- лимит < 100% и `was_limited=1` → уведомление «сбросился», `was_limited=0`.
Уведомления отправляются только если `notify_enabled`.

## Ошибки

- `invalid_grant` при refresh → `needs_relogin=1`, одно уведомление владельцу с кнопкой «Войти заново» (запускает FSM и заменяет токены в том же аккаунте).
- 429/5xx → повтор с backoff; ошибка в `last_error`, без уведомления.
- Неверный/просроченный код или state → понятное сообщение + «Начать заново».

## Тестирование

pytest + pytest-asyncio:
- `crypto`: round-trip;
- PKCE и парсинг кода (3 формата);
- `db` на временном файле: одноразовость и TTL инвайтов, изоляция аккаунтов между пользователями, переходы «упёрся»/«сбросился»;
- `oauth`/`usage` с замоканным HTTP;
- middleware: незнакомый пользователь и невалидный инвайт не получают ответа.

## Развёртывание

Dockerfile: `pip install -r requirements.txt`, `DB_PATH` на volume (Coolify persistent storage). README переписывается под новые env и флоу.
