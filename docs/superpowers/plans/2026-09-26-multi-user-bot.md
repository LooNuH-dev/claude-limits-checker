# Многопользовательский бот — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить однопользовательский `claude_checker.py` на закрытый многопользовательский Telegram-бот, где пользователи по инвайтам добавляют свои аккаунты Claude через OAuth и получают уведомления о лимитах.

**Architecture:** Пакет `app/` на asyncio: aiogram 3 (polling, inline-меню, FSM), aiosqlite (одна БД), Fernet для токенов, aiohttp для Anthropic API. Фоновая задача-монитор в том же event loop. Бизнес-логика (db, oauth, usage, monitor) не зависит от aiogram и тестируется отдельно.

**Tech Stack:** Python 3.11+, aiogram 3.x, aiosqlite, cryptography, aiohttp; тесты — pytest, pytest-asyncio, aioresponses.

**Spec:** `docs/superpowers/specs/2026-09-26-multi-user-bot-design.md`

## Global Constraints

- Python ≥ 3.11; зависимости только из `requirements.txt` / `requirements-dev.txt` ниже.
- `OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"`, `REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"`.
- Token endpoints (по порядку): `https://console.anthropic.com/v1/oauth/token`, `https://platform.claude.com/v1/oauth/token`.
- Usage endpoint: `https://api.anthropic.com/api/oauth/usage`, `User-Agent: claude-code/0.2.29`, `anthropic-version: 2023-06-01`.
- Env: `BOT_TOKEN`, `ADMIN_TELEGRAM_ID`, `ENCRYPTION_KEY` (обязательные), `DB_PATH` (по умолч. `/data/bot.db`), `CHECK_INTERVAL` (сек, по умолч. 300, минимум 60), `INVITE_TTL` (сек, по умолч. 86400).
- TTL `oauth_pending` = 600 сек.
- Неизвестным/заблокированным пользователям бот не отвечает ничем.
- Все токены и `code_verifier` в БД — только зашифрованные.
- Время в БД — целые unix-секунды. Отображение — UTC+2 (как сейчас).
- Все тексты бота — на русском, parse_mode HTML, пользовательские строки через `html.escape`.
- Коммиты — прямо в `main`, без пуша.

## Файловая структура

```
requirements.txt, requirements-dev.txt, pytest.ini
app/__init__.py
app/config.py        Settings из env
app/crypto.py        Cipher (Fernet), генерация ключа
app/db.py            Repo: схема + все запросы; dataclass User, Account
app/oauth.py         PKCE, authorize URL, парсинг ввода кода, exchange/refresh
app/usage.py         запрос usage + refresh/relogin-логика
app/report.py        HTML-форматирование отчётов и уведомлений
app/monitor.py       transition(), check_account(), run_monitor()
app/bot/__init__.py
app/bot/callbacks.py CallbackData-фабрики
app/bot/keyboards.py клавиатуры
app/bot/access.py    AccessMiddleware
app/bot/start.py     /start и инвайты
app/bot/menu.py      меню, статус, аккаунты, карточка, rename/notify/delete
app/bot/add_account.py FSM добавления/перелогина
app/bot/admin.py     инвайты, пользователи, отзыв, диагностика
app/main.py          сборка и запуск
tests/...
```
Удаляются: `claude_checker.py`, `config.example.json`. Переписываются: `Dockerfile`, `docker-compose.yml`, `README.md`, `.gitignore`.

---

### Task 0: Проверка подмены redirect_uri (spike, без кода в репо)

Цель — подтвердить, что произвольный `redirect_uri` отклоняется, а ручной callback работает.

- [ ] **Step 1: Сгенерировать две ссылки**

```bash
python3 - <<'EOF'
import base64, hashlib, secrets, urllib.parse
v = secrets.token_urlsafe(64)
c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
for r in ["https://t.me/example_bot", "https://console.anthropic.com/oauth/code/callback"]:
    print(r, "\n", "https://claude.ai/oauth/authorize?" + urllib.parse.urlencode({
        "code": "true", "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        "response_type": "code", "redirect_uri": r, "scope": "user:profile user:inference",
        "code_challenge": c, "code_challenge_method": "S256", "state": "probe"}), "\n")
EOF
```

- [ ] **Step 2: Открыть обе ссылки в браузере (пользователь, залогиненный в claude.ai)**

Expected: для `t.me` — ошибка «invalid redirect_uri» (или аналог) до экрана Authorize; для callback — экран Authorize, после него страница с кодом вида `xxxx#probe`.

- [ ] **Step 3: Зафиксировать результат**

Если подмена на `t.me` **сработала** — остановиться и сообщить пользователю (флоу меняется, план пересматривается). Иначе продолжать. Если scope `user:inference` отклонён — повторить со `scope=user:profile` и использовать его как `SCOPES` в Task 3.

---

### Task 1: Каркас проекта, config, crypto

**Files:**
- Create: `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, `app/__init__.py`, `app/config.py`, `app/crypto.py`, `tests/__init__.py`, `tests/test_config.py`, `tests/test_crypto.py`

**Interfaces:**
- Produces: `Settings` (поля `bot_token: str, admin_id: int, encryption_key: str, db_path: str, check_interval: int, invite_ttl: int`), `load_settings(env: Mapping[str,str]) -> Settings`, `Cipher(key: str)` с `encrypt(str)->str`, `decrypt(str)->str`, `generate_key() -> str`.

- [ ] **Step 1: Зависимости и конфиг pytest**

`requirements.txt`:
```
aiogram>=3.13,<4
aiosqlite>=0.20
cryptography>=43
aiohttp>=3.9
```
`requirements-dev.txt`:
```
-r requirements.txt
pytest>=8
pytest-asyncio>=0.24
aioresponses>=0.7.6
```
`pytest.ini`:
```ini
[pytest]
asyncio_mode = auto
asyncio_default_fixture_loop_scope = function
testpaths = tests
```
`app/__init__.py`, `tests/__init__.py` — пустые.

Run: `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
Добавить в `.gitignore` строку `.venv/` и `*.db`.

- [ ] **Step 2: Падающие тесты**

`tests/test_config.py`:
```python
import pytest
from app.config import load_settings


def test_load_settings_defaults():
    s = load_settings({"BOT_TOKEN": "t", "ADMIN_TELEGRAM_ID": "42", "ENCRYPTION_KEY": "k"})
    assert s.admin_id == 42
    assert s.db_path == "/data/bot.db"
    assert s.check_interval == 300
    assert s.invite_ttl == 86400


def test_check_interval_floor():
    s = load_settings({"BOT_TOKEN": "t", "ADMIN_TELEGRAM_ID": "1", "ENCRYPTION_KEY": "k", "CHECK_INTERVAL": "5"})
    assert s.check_interval == 60


def test_missing_required():
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        load_settings({"BOT_TOKEN": "t", "ADMIN_TELEGRAM_ID": "1"})
```
`tests/test_crypto.py`:
```python
import pytest
from cryptography.fernet import InvalidToken
from app.crypto import Cipher, generate_key


def test_roundtrip():
    c = Cipher(generate_key())
    enc = c.encrypt("sk-ant-ort01-secret")
    assert "secret" not in enc
    assert c.decrypt(enc) == "sk-ant-ort01-secret"


def test_wrong_key_fails():
    enc = Cipher(generate_key()).encrypt("x")
    with pytest.raises(InvalidToken):
        Cipher(generate_key()).decrypt(enc)
```

- [ ] **Step 3: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_config.py tests/test_crypto.py -v`
Expected: FAIL (`ModuleNotFoundError: app.config`).

- [ ] **Step 4: Реализация**

`app/config.py`:
```python
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
```
`app/crypto.py`:
```python
from cryptography.fernet import Fernet


class Cipher:
    def __init__(self, key: str):
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        return self._fernet.decrypt(token.encode()).decode()


def generate_key() -> str:
    return Fernet.generate_key().decode()


if __name__ == "__main__":
    # python -m app.crypto — сгенерировать ENCRYPTION_KEY
    print(generate_key())
```

- [ ] **Step 5: Тесты проходят**

Run: `.venv/bin/pytest tests/test_config.py tests/test_crypto.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add requirements*.txt pytest.ini .gitignore app tests
git commit -m "feat: каркас app/, настройки из env и шифрование токенов"
```

---

### Task 2: База данных (Repo)

**Files:**
- Create: `app/db.py`, `tests/conftest.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: `Cipher`.
- Produces (все методы `async`, `now: int | None = None` — для тестов):
  - `Repo.open(path: str, cipher: Cipher) -> Repo` (classmethod), `close()`
  - `upsert_admin(tg_id: int)`, `get_user(tg_id) -> User | None`, `list_users() -> list[User]`, `block_user(tg_id) -> bool`
  - `create_invite(created_by: int, ttl: int, now=None) -> str`, `redeem_invite(token: str, tg_id: int, username: str | None, now=None) -> bool`
  - `add_account(owner: int, label: str, email: str | None, access: str, refresh: str, expires_at: int | None, now=None) -> int`
  - `update_tokens(account_id: int, access: str, refresh: str, expires_at: int | None)` (сбрасывает `needs_relogin`)
  - `get_account(account_id: int, owner: int | None = None) -> Account | None`, `list_accounts(owner: int) -> list[Account]`, `list_active_accounts() -> list[Account]`
  - `rename_account(account_id, owner, label) -> bool`, `toggle_notify(account_id, owner) -> bool | None` (новое значение), `delete_account(account_id, owner) -> bool`, `set_needs_relogin(account_id)`
  - `get_was_limited(account_id) -> bool`, `save_check(account_id, percent: float | None, was_limited: bool, error: str | None, now=None)`, `get_state(account_id) -> dict | None`
  - `save_pending(state: str, tg_id: int, verifier: str, account_id: int | None, now=None)`, `pop_pending(state: str, tg_id: int, now=None) -> tuple[str, int | None] | None`
  - `stats() -> dict` (ключи `users`, `accounts`, `needs_relogin`, `errors: list[tuple[str, str]]`)
  - `User(tg_id, username, role, blocked)` с `is_admin`; `Account(id, owner_tg_id, label, email, access_token, refresh_token, expires_at, notify_enabled, needs_relogin)`.

- [ ] **Step 1: Фикстуры**

`tests/conftest.py`:
```python
import pytest
from app.crypto import Cipher, generate_key
from app.db import Repo


@pytest.fixture
def cipher():
    return Cipher(generate_key())


@pytest.fixture
async def repo(tmp_path, cipher):
    r = await Repo.open(str(tmp_path / "t.db"), cipher)
    yield r
    await r.close()
```

- [ ] **Step 2: Падающие тесты**

`tests/test_db.py`:
```python
async def test_admin_upsert(repo):
    await repo.upsert_admin(1)
    u = await repo.get_user(1)
    assert u.is_admin and not u.blocked


async def test_invite_one_time(repo):
    await repo.upsert_admin(1)
    tok = await repo.create_invite(1, ttl=100, now=1000)
    assert await repo.redeem_invite(tok, 2, "bob", now=1050)
    assert not await repo.redeem_invite(tok, 3, "eve", now=1051)
    assert (await repo.get_user(2)).role == "user"
    assert await repo.get_user(3) is None


async def test_invite_expired(repo):
    tok = await repo.create_invite(1, ttl=100, now=1000)
    assert not await repo.redeem_invite(tok, 2, None, now=1100)
    assert not await repo.redeem_invite("nope", 2, None, now=1000)


async def test_accounts_isolated_and_encrypted(repo, tmp_path):
    await repo.upsert_admin(1)
    tok = await repo.create_invite(1, 100, now=0)
    await repo.redeem_invite(tok, 2, None, now=1)
    a = await repo.add_account(1, "mine", "a@x", "acc-SECRET", "ref-SECRET", 500)
    assert await repo.get_account(a, owner=2) is None
    assert (await repo.get_account(a, owner=1)).refresh_token == "ref-SECRET"
    assert await repo.list_accounts(2) == []
    raw = (tmp_path / "t.db").read_bytes()
    assert b"SECRET" not in raw


async def test_account_ops(repo):
    await repo.upsert_admin(1)
    a = await repo.add_account(1, "x", None, "a", "r", None)
    assert await repo.rename_account(a, 1, "new")
    assert await repo.toggle_notify(a, 1) is False
    assert await repo.toggle_notify(a, 1) is True
    await repo.set_needs_relogin(a)
    assert await repo.list_active_accounts() == []
    await repo.update_tokens(a, "a2", "r2", 10)
    acc = (await repo.list_active_accounts())[0]
    assert acc.label == "new" and acc.access_token == "a2" and not acc.needs_relogin
    assert not await repo.delete_account(a, 2)
    assert await repo.delete_account(a, 1)
    assert await repo.get_account(a) is None


async def test_block_user_removes_accounts(repo):
    await repo.upsert_admin(1)
    tok = await repo.create_invite(1, 100, now=0)
    await repo.redeem_invite(tok, 2, None, now=1)
    a = await repo.add_account(2, "x", None, "a", "r", None)
    await repo.save_check(a, 50.0, False, None)
    assert await repo.block_user(2)
    assert (await repo.get_user(2)).blocked
    assert await repo.get_account(a) is None
    assert await repo.get_state(a) is None
    assert not await repo.block_user(1)  # админа не блокируем


async def test_state(repo):
    await repo.upsert_admin(1)
    a = await repo.add_account(1, "x", None, "a", "r", None)
    assert await repo.get_was_limited(a) is False
    await repo.save_check(a, 100.0, True, None, now=5)
    assert await repo.get_was_limited(a) is True
    await repo.save_check(a, None, True, "boom", now=6)
    st = await repo.get_state(a)
    assert st["last_error"] == "boom" and st["last_5h_percent"] == 100.0


async def test_pending(repo):
    await repo.save_pending("s1", 7, "verifier", None, now=100)
    assert await repo.pop_pending("s1", 8, now=101) is None       # чужой tg_id
    assert await repo.pop_pending("s1", 7, now=101) == ("verifier", None)
    assert await repo.pop_pending("s1", 7, now=102) is None       # одноразово
    await repo.save_pending("s2", 7, "v", 3, now=100)
    assert await repo.pop_pending("s2", 7, now=701) is None       # TTL 600
```

- [ ] **Step 3: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: FAIL (`ModuleNotFoundError: app.db`).

- [ ] **Step 4: Реализация**

`app/db.py`:
```python
import secrets
import time
from dataclasses import dataclass

import aiosqlite

from app.crypto import Cipher

PENDING_TTL = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    role TEXT NOT NULL DEFAULT 'user',
    invited_by INTEGER,
    created_at INTEGER NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invites(
    token TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_by INTEGER,
    used_at INTEGER
);
CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_tg_id INTEGER NOT NULL REFERENCES users(tg_id),
    label TEXT NOT NULL,
    email TEXT,
    access_token_enc TEXT NOT NULL,
    refresh_token_enc TEXT NOT NULL,
    expires_at INTEGER,
    notify_enabled INTEGER NOT NULL DEFAULT 1,
    needs_relogin INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS account_state(
    account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    last_5h_percent REAL,
    was_limited INTEGER NOT NULL DEFAULT 0,
    last_checked INTEGER,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS oauth_pending(
    state TEXT PRIMARY KEY,
    tg_id INTEGER NOT NULL,
    code_verifier_enc TEXT NOT NULL,
    account_id INTEGER,
    expires_at INTEGER NOT NULL
);
"""
SCHEMA_VERSION = 1


def _now(now: int | None) -> int:
    return int(time.time()) if now is None else now


@dataclass
class User:
    tg_id: int
    username: str | None
    role: str
    blocked: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


@dataclass
class Account:
    id: int
    owner_tg_id: int
    label: str
    email: str | None
    access_token: str
    refresh_token: str
    expires_at: int | None
    notify_enabled: bool
    needs_relogin: bool


class Repo:
    def __init__(self, db: aiosqlite.Connection, cipher: Cipher):
        self.db = db
        self.cipher = cipher

    @classmethod
    async def open(cls, path: str, cipher: Cipher) -> "Repo":
        db = await aiosqlite.connect(path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA journal_mode=WAL")
        async with db.execute("PRAGMA user_version") as cur:
            version = (await cur.fetchone())[0]
        if version < SCHEMA_VERSION:
            await db.executescript(SCHEMA)
            await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            await db.commit()
        return cls(db, cipher)

    async def close(self) -> None:
        await self.db.close()

    # --- users ---

    @staticmethod
    def _user(row) -> User:
        return User(row["tg_id"], row["username"], row["role"], bool(row["blocked"]))

    async def upsert_admin(self, tg_id: int) -> None:
        await self.db.execute(
            "INSERT INTO users(tg_id, role, created_at) VALUES(?, 'admin', ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET role='admin', blocked=0",
            (tg_id, _now(None)),
        )
        await self.db.commit()

    async def get_user(self, tg_id: int) -> User | None:
        async with self.db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)) as cur:
            row = await cur.fetchone()
        return self._user(row) if row else None

    async def list_users(self) -> list[User]:
        async with self.db.execute("SELECT * FROM users ORDER BY created_at") as cur:
            return [self._user(r) for r in await cur.fetchall()]

    async def block_user(self, tg_id: int) -> bool:
        cur = await self.db.execute(
            "UPDATE users SET blocked=1 WHERE tg_id=? AND role!='admin'", (tg_id,)
        )
        if cur.rowcount != 1:
            return False
        await self.db.execute("DELETE FROM accounts WHERE owner_tg_id=?", (tg_id,))
        await self.db.execute("DELETE FROM oauth_pending WHERE tg_id=?", (tg_id,))
        await self.db.commit()
        return True

    # --- invites ---

    async def create_invite(self, created_by: int, ttl: int, now: int | None = None) -> str:
        token = secrets.token_urlsafe(16)
        await self.db.execute(
            "INSERT INTO invites(token, created_by, expires_at) VALUES(?,?,?)",
            (token, created_by, _now(now) + ttl),
        )
        await self.db.commit()
        return token

    async def redeem_invite(self, token: str, tg_id: int, username: str | None,
                            now: int | None = None) -> bool:
        now = _now(now)
        cur = await self.db.execute(
            "UPDATE invites SET used_by=?, used_at=? "
            "WHERE token=? AND used_by IS NULL AND expires_at>?",
            (tg_id, now, token, now),
        )
        if cur.rowcount != 1:
            return False
        await self.db.execute(
            "INSERT INTO users(tg_id, username, role, invited_by, created_at) "
            "VALUES(?, ?, 'user', (SELECT created_by FROM invites WHERE token=?), ?) "
            "ON CONFLICT(tg_id) DO UPDATE SET blocked=0, username=excluded.username",
            (tg_id, username, token, now),
        )
        await self.db.commit()
        return True

    # --- accounts ---

    def _account(self, row) -> Account:
        return Account(
            id=row["id"],
            owner_tg_id=row["owner_tg_id"],
            label=row["label"],
            email=row["email"],
            access_token=self.cipher.decrypt(row["access_token_enc"]),
            refresh_token=self.cipher.decrypt(row["refresh_token_enc"]),
            expires_at=row["expires_at"],
            notify_enabled=bool(row["notify_enabled"]),
            needs_relogin=bool(row["needs_relogin"]),
        )

    async def add_account(self, owner: int, label: str, email: str | None, access: str,
                          refresh: str, expires_at: int | None, now: int | None = None) -> int:
        cur = await self.db.execute(
            "INSERT INTO accounts(owner_tg_id, label, email, access_token_enc, "
            "refresh_token_enc, expires_at, created_at) VALUES(?,?,?,?,?,?,?)",
            (owner, label, email, self.cipher.encrypt(access), self.cipher.encrypt(refresh),
             expires_at, _now(now)),
        )
        await self.db.commit()
        return cur.lastrowid

    async def update_tokens(self, account_id: int, access: str, refresh: str,
                            expires_at: int | None) -> None:
        await self.db.execute(
            "UPDATE accounts SET access_token_enc=?, refresh_token_enc=?, expires_at=?, "
            "needs_relogin=0 WHERE id=?",
            (self.cipher.encrypt(access), self.cipher.encrypt(refresh), expires_at, account_id),
        )
        await self.db.commit()

    async def get_account(self, account_id: int, owner: int | None = None) -> Account | None:
        sql, args = "SELECT * FROM accounts WHERE id=?", [account_id]
        if owner is not None:
            sql += " AND owner_tg_id=?"
            args.append(owner)
        async with self.db.execute(sql, args) as cur:
            row = await cur.fetchone()
        return self._account(row) if row else None

    async def list_accounts(self, owner: int) -> list[Account]:
        async with self.db.execute(
            "SELECT * FROM accounts WHERE owner_tg_id=? ORDER BY id", (owner,)
        ) as cur:
            return [self._account(r) for r in await cur.fetchall()]

    async def list_active_accounts(self) -> list[Account]:
        async with self.db.execute(
            "SELECT a.* FROM accounts a JOIN users u ON u.tg_id=a.owner_tg_id "
            "WHERE a.needs_relogin=0 AND u.blocked=0 ORDER BY a.id"
        ) as cur:
            return [self._account(r) for r in await cur.fetchall()]

    async def rename_account(self, account_id: int, owner: int, label: str) -> bool:
        cur = await self.db.execute(
            "UPDATE accounts SET label=? WHERE id=? AND owner_tg_id=?", (label, account_id, owner)
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def toggle_notify(self, account_id: int, owner: int) -> bool | None:
        await self.db.execute(
            "UPDATE accounts SET notify_enabled=1-notify_enabled WHERE id=? AND owner_tg_id=?",
            (account_id, owner),
        )
        await self.db.commit()
        acc = await self.get_account(account_id, owner)
        return acc.notify_enabled if acc else None

    async def delete_account(self, account_id: int, owner: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM accounts WHERE id=? AND owner_tg_id=?", (account_id, owner)
        )
        await self.db.commit()
        return cur.rowcount == 1

    async def set_needs_relogin(self, account_id: int) -> None:
        await self.db.execute("UPDATE accounts SET needs_relogin=1 WHERE id=?", (account_id,))
        await self.db.commit()

    # --- state ---

    async def get_state(self, account_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM account_state WHERE account_id=?", (account_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_was_limited(self, account_id: int) -> bool:
        st = await self.get_state(account_id)
        return bool(st and st["was_limited"])

    async def save_check(self, account_id: int, percent: float | None, was_limited: bool,
                         error: str | None, now: int | None = None) -> None:
        # percent=None (ошибка) не затирает последнее известное значение
        await self.db.execute(
            "INSERT INTO account_state(account_id, last_5h_percent, was_limited, last_checked, last_error) "
            "VALUES(?,?,?,?,?) ON CONFLICT(account_id) DO UPDATE SET "
            "last_5h_percent=COALESCE(excluded.last_5h_percent, last_5h_percent), "
            "was_limited=excluded.was_limited, last_checked=excluded.last_checked, "
            "last_error=excluded.last_error",
            (account_id, percent, int(was_limited), _now(now), error),
        )
        await self.db.commit()

    # --- oauth pending ---

    async def save_pending(self, state: str, tg_id: int, verifier: str,
                           account_id: int | None, now: int | None = None) -> None:
        now = _now(now)
        await self.db.execute("DELETE FROM oauth_pending WHERE expires_at<=?", (now,))
        await self.db.execute(
            "INSERT OR REPLACE INTO oauth_pending(state, tg_id, code_verifier_enc, account_id, expires_at) "
            "VALUES(?,?,?,?,?)",
            (state, tg_id, self.cipher.encrypt(verifier), account_id, now + PENDING_TTL),
        )
        await self.db.commit()

    async def pop_pending(self, state: str, tg_id: int,
                          now: int | None = None) -> tuple[str, int | None] | None:
        async with self.db.execute(
            "SELECT * FROM oauth_pending WHERE state=? AND tg_id=? AND expires_at>?",
            (state, tg_id, _now(now)),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await self.db.execute("DELETE FROM oauth_pending WHERE state=?", (state,))
        await self.db.commit()
        return self.cipher.decrypt(row["code_verifier_enc"]), row["account_id"]

    # --- diagnostics ---

    async def stats(self) -> dict:
        async def scalar(sql):
            async with self.db.execute(sql) as cur:
                return (await cur.fetchone())[0]
        async with self.db.execute(
            "SELECT a.label, s.last_error FROM account_state s JOIN accounts a ON a.id=s.account_id "
            "WHERE s.last_error IS NOT NULL"
        ) as cur:
            errors = [(r[0], r[1]) for r in await cur.fetchall()]
        return {
            "users": await scalar("SELECT COUNT(*) FROM users WHERE blocked=0"),
            "accounts": await scalar("SELECT COUNT(*) FROM accounts"),
            "needs_relogin": await scalar("SELECT COUNT(*) FROM accounts WHERE needs_relogin=1"),
            "errors": errors,
        }
```

- [ ] **Step 5: Тесты проходят**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add app/db.py tests/conftest.py tests/test_db.py
git commit -m "feat: SQLite-репозиторий пользователей, инвайтов и аккаунтов"
```

---

### Task 3: OAuth (PKCE, парсинг кода, exchange/refresh)

**Files:**
- Create: `app/oauth.py`, `tests/test_oauth.py`

**Interfaces:**
- Produces: `make_pkce() -> tuple[str, str]` (verifier, challenge), `new_state() -> str`, `build_authorize_url(challenge: str, state: str) -> str`, `parse_code_input(text: str) -> tuple[str, str | None]` (ValueError при пустом), `TokenSet(access_token, refresh_token, expires_at: int | None, email: str | None)`, `OAuthError`, `InvalidGrant(OAuthError)`, `async exchange_code(http: aiohttp.ClientSession, code, state, verifier) -> TokenSet`, `async refresh_tokens(http, refresh_token) -> TokenSet`, константы `TOKEN_URLS`, `USER_AGENT`.

- [ ] **Step 1: Падающие тесты**

`tests/test_oauth.py`:
```python
import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from aioresponses import aioresponses

from app import oauth


def test_pkce_s256():
    v, c = oauth.make_pkce()
    assert 43 <= len(v) <= 128
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def test_authorize_url():
    q = parse_qs(urlparse(oauth.build_authorize_url("CH", "ST")).query)
    assert q["client_id"] == [oauth.CLIENT_ID]
    assert q["redirect_uri"] == [oauth.REDIRECT_URI]
    assert q["code_challenge"] == ["CH"] and q["state"] == ["ST"]
    assert q["code_challenge_method"] == ["S256"]


@pytest.mark.parametrize("text,expected", [
    ("abc#st", ("abc", "st")),
    ("  abc  ", ("abc", None)),
    ("https://console.anthropic.com/oauth/code/callback?code=abc&state=st", ("abc", "st")),
    ("https://console.anthropic.com/oauth/code/callback?code=abc#st", ("abc", "st")),
])
def test_parse_code_input(text, expected):
    assert oauth.parse_code_input(text) == expected


def test_parse_code_empty():
    with pytest.raises(ValueError):
        oauth.parse_code_input("   ")


async def test_exchange_code():
    with aioresponses() as m:
        m.post(oauth.TOKEN_URLS[0], payload={
            "access_token": "A", "refresh_token": "R", "expires_in": 3600,
            "account": {"email_address": "u@x.com"},
        })
        async with aiohttp.ClientSession() as http:
            ts = await oauth.exchange_code(http, "c", "s", "v")
        body = list(m.requests.values())[0][0].kwargs["json"]
    assert ts.access_token == "A" and ts.refresh_token == "R" and ts.email == "u@x.com"
    assert ts.expires_at is not None
    assert body["grant_type"] == "authorization_code" and body["code_verifier"] == "v"


async def test_refresh_falls_back_and_keeps_refresh():
    with aioresponses() as m:
        m.post(oauth.TOKEN_URLS[0], status=404, body="not found")
        m.post(oauth.TOKEN_URLS[1], payload={"access_token": "A2"})
        async with aiohttp.ClientSession() as http:
            ts = await oauth.refresh_tokens(http, "R1")
    assert ts.access_token == "A2" and ts.refresh_token == "R1"


async def test_invalid_grant():
    with aioresponses() as m:
        m.post(oauth.TOKEN_URLS[0], status=400, payload={"error": "invalid_grant"})
        async with aiohttp.ClientSession() as http:
            with pytest.raises(oauth.InvalidGrant):
                await oauth.refresh_tokens(http, "dead")
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_oauth.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Реализация**

`app/oauth.py`:
```python
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPES = "user:profile user:inference"
TOKEN_URLS = [
    "https://console.anthropic.com/v1/oauth/token",
    "https://platform.claude.com/v1/oauth/token",
]
USER_AGENT = "claude-code/0.2.29"
TIMEOUT = aiohttp.ClientTimeout(total=15)


class OAuthError(Exception):
    pass


class InvalidGrant(OAuthError):
    pass


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: int | None
    email: str | None


def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(24)


def build_authorize_url(challenge: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })


def parse_code_input(text: str) -> tuple[str, str | None]:
    """Принимает `code#state`, голый код или URL callback-страницы целиком."""
    text = text.strip()
    if text.startswith("http"):
        parsed = urlparse(text)
        q = parse_qs(parsed.query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [None])[0] or (parsed.fragment or None)
    elif "#" in text:
        code, state = text.split("#", 1)
        state = state or None
    else:
        code, state = text, None
    if not code:
        raise ValueError("пустой код")
    return code, state


async def _post_token(http: aiohttp.ClientSession, payload: dict,
                      fallback_refresh: str | None = None) -> TokenSet:
    last_err: Exception | None = None
    headers = {"Content-Type": "application/json", "Accept": "application/json",
               "User-Agent": USER_AGENT}
    for url in TOKEN_URLS:
        try:
            async with http.post(url, json=payload, headers=headers, timeout=TIMEOUT) as resp:
                raw = await resp.text()
                if resp.status >= 400:
                    if "invalid_grant" in raw.lower():
                        raise InvalidGrant("Сессия истекла или код недействителен (invalid_grant)")
                    last_err = OAuthError(f"HTTP {resp.status} на {url}: {raw[:300] or 'без деталей'}")
                    # 404/405/400 без invalid_grant — возможно, эндпоинт переехал
                    if resp.status in (400, 404, 405):
                        continue
                    raise last_err
                data = json.loads(raw)
        except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as e:
            last_err = OAuthError(f"Сеть/ответ {url}: {e}")
            continue
        access = data.get("access_token")
        if not access:
            raise OAuthError("Ответ OAuth не содержит access_token")
        expires_in = data.get("expires_in")
        return TokenSet(
            access_token=access,
            refresh_token=data.get("refresh_token") or fallback_refresh or "",
            expires_at=int(time.time()) + int(expires_in) if expires_in else None,
            email=(data.get("account") or {}).get("email_address"),
        )
    raise last_err or OAuthError("Не удалось получить токен")


async def exchange_code(http: aiohttp.ClientSession, code: str, state: str,
                        verifier: str) -> TokenSet:
    return await _post_token(http, {
        "grant_type": "authorization_code",
        "code": code,
        "state": state,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    })


async def refresh_tokens(http: aiohttp.ClientSession, refresh_token: str) -> TokenSet:
    return await _post_token(http, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }, fallback_refresh=refresh_token)
```

- [ ] **Step 4: Тесты проходят**

Run: `.venv/bin/pytest tests/test_oauth.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add app/oauth.py tests/test_oauth.py
git commit -m "feat: OAuth PKCE-флоу Claude Code с обменом кода и refresh"
```

---

### Task 4: Usage + отчёты

**Files:**
- Create: `app/usage.py`, `app/report.py`, `tests/test_usage.py`, `tests/test_report.py`

**Interfaces:**
- Consumes: `Repo`, `Account`, `oauth.refresh_tokens`, `oauth.InvalidGrant`, `oauth.USER_AGENT`.
- Produces:
  - `usage.USAGE_URL`, `usage.NeedsRelogin(Exception)`, `async fetch_usage(http, access_token, retries=2) -> dict`, `async fetch_account_usage(http, repo, account: Account) -> dict` (рефрешит при истечении/401, сохраняет токены; при `InvalidGrant` — `repo.set_needs_relogin` и `raise NeedsRelogin`).
  - `report.five_hour(usage: dict) -> tuple[float, datetime | None]`, `report.format_account(label: str, usage: dict | None, error: str | None) -> str`, `report.format_status(items: list[tuple[str, dict | None, str | None]]) -> str`, `report.limit_reached_text(label, reset_dt)`, `report.reset_text(label, percent)`, `report.relogin_text(label)`.

- [ ] **Step 1: Падающие тесты**

`tests/test_usage.py`:
```python
import aiohttp
import pytest
from aioresponses import aioresponses

from app import oauth, usage

USAGE = {"five_hour": {"utilization": 42.0, "resets_at": None}}


async def _acc(repo, expires_at=None):
    await repo.upsert_admin(1)
    aid = await repo.add_account(1, "x", None, "OLD", "R", expires_at)
    return await repo.get_account(aid)


async def test_fetch_ok(repo):
    acc = await _acc(repo)
    with aioresponses() as m:
        m.get(usage.USAGE_URL, payload=USAGE)
        async with aiohttp.ClientSession() as http:
            assert await usage.fetch_account_usage(http, repo, acc) == USAGE


async def test_401_refreshes_and_saves(repo):
    acc = await _acc(repo)
    with aioresponses() as m:
        m.get(usage.USAGE_URL, status=401)
        m.post(oauth.TOKEN_URLS[0], payload={"access_token": "NEW", "refresh_token": "R2"})
        m.get(usage.USAGE_URL, payload=USAGE)
        async with aiohttp.ClientSession() as http:
            assert await usage.fetch_account_usage(http, repo, acc) == USAGE
    saved = await repo.get_account(acc.id)
    assert saved.access_token == "NEW" and saved.refresh_token == "R2"


async def test_expired_refreshes_first(repo):
    acc = await _acc(repo, expires_at=1)
    with aioresponses() as m:
        m.post(oauth.TOKEN_URLS[0], payload={"access_token": "NEW"})
        m.get(usage.USAGE_URL, payload=USAGE)
        async with aiohttp.ClientSession() as http:
            await usage.fetch_account_usage(http, repo, acc)
    assert (await repo.get_account(acc.id)).access_token == "NEW"


async def test_invalid_grant_marks_relogin(repo):
    acc = await _acc(repo)
    with aioresponses() as m:
        m.get(usage.USAGE_URL, status=401)
        m.post(oauth.TOKEN_URLS[0], status=400, payload={"error": "invalid_grant"})
        async with aiohttp.ClientSession() as http:
            with pytest.raises(usage.NeedsRelogin):
                await usage.fetch_account_usage(http, repo, acc)
    assert (await repo.get_account(acc.id)).needs_relogin


async def test_429_retry(repo, monkeypatch):
    async def no_sleep(_):
        return None
    monkeypatch.setattr(usage.asyncio, "sleep", no_sleep)
    with aioresponses() as m:
        m.get(usage.USAGE_URL, status=429, headers={"retry-after": "1"})
        m.get(usage.USAGE_URL, payload=USAGE)
        async with aiohttp.ClientSession() as http:
            assert await usage.fetch_usage(http, "T") == USAGE
```
`tests/test_report.py`:
```python
from app import report


def test_five_hour_parsing():
    p, dt = report.five_hour({"five_hour": {"utilization": 100, "resets_at": "2030-01-01T00:00:00Z"}})
    assert p == 100.0 and dt.year == 2030
    assert report.five_hour({}) == (0.0, None)


def test_status_escapes_and_errors():
    text = report.format_status([
        ("<b>evil</b>", {"five_hour": {"utilization": 10}, "seven_day": {"utilization": 20}}, None),
        ("broken", None, "HTTP 500"),
    ])
    assert "&lt;b&gt;evil" in text
    assert "10.0%" in text and "20.0%" in text
    assert "HTTP 500" in text


def test_status_empty():
    assert "Нет аккаунтов" in report.format_status([])
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_usage.py tests/test_report.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Реализация**

`app/usage.py`:
```python
import asyncio
import time

import aiohttp

from app import oauth
from app.db import Account, Repo

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TIMEOUT = aiohttp.ClientTimeout(total=10)
REFRESH_SKEW = 60


class NeedsRelogin(Exception):
    pass


class _Unauthorized(Exception):
    pass


async def fetch_usage(http: aiohttp.ClientSession, access_token: str, retries: int = 2) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "anthropic-version": "2023-06-01",
        "User-Agent": oauth.USER_AGENT,
        "Content-Type": "application/json",
    }
    for attempt in range(retries + 1):
        async with http.get(USAGE_URL, headers=headers, timeout=TIMEOUT) as resp:
            if resp.status == 401:
                raise _Unauthorized()
            if resp.status in (429, 500, 502, 503, 504) and attempt < retries:
                try:
                    wait = float(resp.headers.get("retry-after", ""))
                except ValueError:
                    wait = 2.0 * (attempt + 1)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            return await resp.json()
    raise RuntimeError("unreachable")


async def _refresh(http: aiohttp.ClientSession, repo: Repo, account: Account) -> str:
    try:
        ts = await oauth.refresh_tokens(http, account.refresh_token)
    except oauth.InvalidGrant as e:
        await repo.set_needs_relogin(account.id)
        raise NeedsRelogin(str(e)) from e
    await repo.update_tokens(account.id, ts.access_token, ts.refresh_token, ts.expires_at)
    account.access_token, account.refresh_token = ts.access_token, ts.refresh_token
    account.expires_at = ts.expires_at
    return ts.access_token


async def fetch_account_usage(http: aiohttp.ClientSession, repo: Repo, account: Account) -> dict:
    token = account.access_token
    if account.expires_at and account.expires_at - REFRESH_SKEW <= time.time():
        token = await _refresh(http, repo, account)
    try:
        return await fetch_usage(http, token)
    except _Unauthorized:
        token = await _refresh(http, repo, account)
        try:
            return await fetch_usage(http, token)
        except _Unauthorized as e:
            await repo.set_needs_relogin(account.id)
            raise NeedsRelogin("Токен не принят после обновления") from e
```
`app/report.py` (перенос форматирования из старого `claude_checker.py`):
```python
from datetime import datetime, timedelta, timezone
from html import escape

DISPLAY_TZ = timezone(timedelta(hours=2))


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
    return "\n".join(parts)


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
```

- [ ] **Step 4: Тесты проходят**

Run: `.venv/bin/pytest tests/test_usage.py tests/test_report.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/usage.py app/report.py tests/test_usage.py tests/test_report.py
git commit -m "feat: запрос лимитов с авто-refresh и форматирование отчётов"
```

---

### Task 5: Монитор

**Files:**
- Create: `app/monitor.py`, `tests/test_monitor.py`

**Interfaces:**
- Consumes: `Repo`, `usage.fetch_account_usage`, `usage.NeedsRelogin`, `report.*`.
- Produces: `transition(was_limited: bool, percent: float) -> str | None` (`"limited"`/`"reset"`/`None`), `Notice(chat_id: int, text: str, relogin_account_id: int | None)`, `async check_account(http, repo, account) -> Notice | None`, `async run_monitor(bot, repo, http, interval: int, send: Callable | None = None)`. Клавиатура для relogin строится в `run_monitor` через `app.bot.keyboards.relogin_kb(account_id)` (Task 6) — импорт внутри функции.

- [ ] **Step 1: Падающие тесты**

`tests/test_monitor.py`:
```python
import pytest

from app import monitor, usage


@pytest.mark.parametrize("was,p,exp", [
    (False, 99.9, None), (False, 100.0, "limited"), (True, 100.0, None), (True, 50.0, "reset"),
])
def test_transition(was, p, exp):
    assert monitor.transition(was, p) == exp


async def _setup(repo, notify=True):
    await repo.upsert_admin(1)
    aid = await repo.add_account(1, "acc", None, "A", "R", None)
    if not notify:
        await repo.toggle_notify(aid, 1)
    return await repo.get_account(aid)


def _fake_usage(monkeypatch, result):
    async def fake(http, repo, account):
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(monitor.usage, "fetch_account_usage", fake)


async def test_limit_then_reset(repo, monkeypatch):
    acc = await _setup(repo)
    _fake_usage(monkeypatch, {"five_hour": {"utilization": 100}})
    n = await monitor.check_account(None, repo, acc)
    assert n.chat_id == 1 and "100%" in n.text
    assert await monitor.check_account(None, repo, acc) is None  # без дублей
    _fake_usage(monkeypatch, {"five_hour": {"utilization": 3}})
    n = await monitor.check_account(None, repo, acc)
    assert "сбросились" in n.text


async def test_notify_disabled_still_tracks_state(repo, monkeypatch):
    acc = await _setup(repo, notify=False)
    _fake_usage(monkeypatch, {"five_hour": {"utilization": 100}})
    assert await monitor.check_account(None, repo, acc) is None
    assert await repo.get_was_limited(acc.id)


async def test_error_saved_without_notice(repo, monkeypatch):
    acc = await _setup(repo)
    _fake_usage(monkeypatch, RuntimeError("HTTP 503"))
    assert await monitor.check_account(None, repo, acc) is None
    assert (await repo.get_state(acc.id))["last_error"] == "HTTP 503"


async def test_relogin_notice(repo, monkeypatch):
    acc = await _setup(repo)
    _fake_usage(monkeypatch, usage.NeedsRelogin("x"))
    n = await monitor.check_account(None, repo, acc)
    assert n.relogin_account_id == acc.id
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_monitor.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Реализация**

`app/monitor.py`:
```python
import asyncio
import logging
from dataclasses import dataclass

from app import report, usage
from app.db import Account, Repo

log = logging.getLogger(__name__)
CONCURRENCY = 5


@dataclass
class Notice:
    chat_id: int
    text: str
    relogin_account_id: int | None = None


def transition(was_limited: bool, percent: float) -> str | None:
    is_limited = percent >= 100.0
    if is_limited and not was_limited:
        return "limited"
    if was_limited and not is_limited:
        return "reset"
    return None


async def check_account(http, repo: Repo, account: Account) -> Notice | None:
    was_limited = await repo.get_was_limited(account.id)
    try:
        data = await usage.fetch_account_usage(http, repo, account)
    except usage.NeedsRelogin as e:
        await repo.save_check(account.id, None, was_limited, f"нужен повторный вход: {e}")
        return Notice(account.owner_tg_id, report.relogin_text(account.label), account.id)
    except Exception as e:  # сеть, 5xx, 429 после ретраев — молча в last_error
        log.warning("[%s] %s", account.id, e)
        await repo.save_check(account.id, None, was_limited, str(e)[:300])
        return None

    percent, reset_dt = report.five_hour(data)
    event = transition(was_limited, percent)
    await repo.save_check(account.id, percent, percent >= 100.0, None)
    if not event or not account.notify_enabled:
        return None
    if event == "limited":
        return Notice(account.owner_tg_id, report.limit_reached_text(account.label, reset_dt))
    return Notice(account.owner_tg_id, report.reset_text(account.label, percent))


async def run_monitor(bot, repo: Repo, http, interval: int) -> None:
    from app.bot.keyboards import relogin_kb

    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(acc: Account):
        async with sem:
            notice = await check_account(http, repo, acc)
        if notice:
            kb = relogin_kb(notice.relogin_account_id) if notice.relogin_account_id else None
            try:
                await bot.send_message(notice.chat_id, notice.text, reply_markup=kb)
            except Exception as e:
                log.warning("не удалось отправить уведомление %s: %s", notice.chat_id, e)

    await asyncio.sleep(5)  # дать боту стартовать
    while True:
        try:
            accounts = await repo.list_active_accounts()
            await asyncio.gather(*(one(a) for a in accounts))
        except Exception:
            log.exception("ошибка цикла мониторинга")
        await asyncio.sleep(interval)
```

- [ ] **Step 4: Тесты проходят**

Run: `.venv/bin/pytest tests/test_monitor.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/monitor.py tests/test_monitor.py
git commit -m "feat: фоновый монитор лимитов с уведомлениями владельцам"
```

---

### Task 6: Бот — callbacks, клавиатуры, доступ, /start

**Files:**
- Create: `app/bot/__init__.py` (пустой), `app/bot/callbacks.py`, `app/bot/keyboards.py`, `app/bot/access.py`, `app/bot/start.py`, `tests/test_access.py`

**Interfaces:**
- Consumes: `Repo`, `User`, `Account`.
- Produces:
  - `callbacks.MenuCb(action: str)` (`home|status|accounts|add|access|cancel`), `callbacks.AccCb(action: str, id: int)` (`open|notify|rename|delete|confirm_delete|relogin`), `callbacks.AdmCb(action: str, tg_id: int = 0)` (`invite|users|revoke|confirm_revoke|diag`).
  - `keyboards.main_menu(is_admin: bool)`, `accounts_list(accounts: list[Account])`, `account_card(acc: Account)`, `confirm_delete(acc_id: int)`, `relogin_kb(acc_id: int)`, `login_kb(url: str)`, `back_home()`, `admin_menu()`, `users_list(users: list[User])`, `confirm_revoke(tg_id: int)`.
  - `access.AccessMiddleware(repo)` — кладёт `data["user"]: User | None`.
  - `start.router`, `start.MENU_TEXT`.

- [ ] **Step 1: Падающие тесты**

`tests/test_access.py`:
```python
from datetime import datetime

from aiogram.types import Chat, Message, User as TgUser

from app.bot.access import AccessMiddleware


def _msg(uid, text):
    return Message(message_id=1, date=datetime.now(), chat=Chat(id=uid, type="private"),
                   from_user=TgUser(id=uid, is_bot=False, first_name="x"), text=text)


async def _run(repo, msg):
    called = {}

    async def handler(event, data):
        called["user"] = data.get("user", "missing")
        return "ok"

    res = await AccessMiddleware(repo)(handler, msg, {})
    return res, called


async def test_unknown_user_dropped(repo):
    res, called = await _run(repo, _msg(99, "hi"))
    assert res is None and called == {}


async def test_blocked_user_dropped(repo):
    await repo.upsert_admin(1)
    tok = await repo.create_invite(1, 100)
    await repo.redeem_invite(tok, 2, None)
    await repo.block_user(2)
    res, called = await _run(repo, _msg(2, "/start"))
    assert called == {}


async def test_invite_start_passes_with_no_user(repo):
    res, called = await _run(repo, _msg(99, "/start inv_abc"))
    assert res == "ok" and called["user"] is None


async def test_known_user_passes(repo):
    await repo.upsert_admin(1)
    res, called = await _run(repo, _msg(1, "hi"))
    assert called["user"].is_admin
```

- [ ] **Step 2: Убедиться, что падают**

Run: `.venv/bin/pytest tests/test_access.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Реализация**

`app/bot/callbacks.py`:
```python
from aiogram.filters.callback_data import CallbackData


class MenuCb(CallbackData, prefix="m"):
    action: str


class AccCb(CallbackData, prefix="a"):
    action: str
    id: int


class AdmCb(CallbackData, prefix="adm"):
    action: str
    tg_id: int = 0
```
`app/bot/keyboards.py`:
```python
from html import escape

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


def confirm_revoke(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        _btn("✅ Отозвать", AdmCb(action="confirm_revoke", tg_id=tg_id)),
        _btn("❌ Отмена", AdmCb(action="users")),
    ]])
```
`app/bot/access.py`:
```python
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from app.db import Repo

INVITE_PREFIX = "/start inv_"


class AccessMiddleware(BaseMiddleware):
    """Пропускает только известных незаблокированных пользователей.

    Единственное исключение — `/start inv_<token>`: его проверяет обработчик,
    и при невалидном токене тоже молчит.
    """

    def __init__(self, repo: Repo):
        self.repo = repo

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user is None:
            return None
        user = await self.repo.get_user(from_user.id)
        if user and not user.blocked:
            data["user"] = user
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith(INVITE_PREFIX):
            data["user"] = None
            return await handler(event, data)
        return None
```
`app/bot/start.py`:
```python
from aiogram import Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from app.bot import keyboards
from app.db import Repo, User

router = Router()
MENU_TEXT = "🤖 <b>Claude Limits Checker</b>\nВыберите действие:"


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, repo: Repo, user: User | None):
    if user is None:
        args = command.args or ""
        if not args.startswith("inv_"):
            return
        ok = await repo.redeem_invite(args.removeprefix("inv_"), message.from_user.id,
                                      message.from_user.username)
        if not ok:
            return  # невалидный инвайт — молчим
        user = await repo.get_user(message.from_user.id)
        await message.answer("✅ Доступ выдан. Добавьте аккаунт Claude, чтобы следить за лимитами.")
    await message.answer(MENU_TEXT, reply_markup=keyboards.main_menu(user.is_admin))
```

- [ ] **Step 4: Тесты проходят**

Run: `.venv/bin/pytest tests/test_access.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/bot tests/test_access.py
git commit -m "feat: закрытый доступ по инвайтам и клавиатуры бота"
```

---

### Task 7: Бот — меню, статус, аккаунты, добавление, админка, запуск

Хендлеры aiogram тонкие и завязаны на Telegram — проверяются ручным smoke-тестом в Step 6.

**Files:**
- Create: `app/bot/menu.py`, `app/bot/add_account.py`, `app/bot/admin.py`, `app/main.py`

**Interfaces:**
- Consumes: всё выше. В handler-kwargs доступны `repo: Repo`, `http: aiohttp.ClientSession`, `settings: Settings`, `user: User`, `state: FSMContext`, `bot: Bot`.
- Produces: `menu.router`, `add_account.router`, `admin.router`, `menu.RenameState`, `add_account.AddState`, `main.main()`.

- [ ] **Step 1: `app/bot/menu.py`**

```python
import asyncio
from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from app import report, usage
from app.bot import keyboards
from app.bot.callbacks import AccCb, MenuCb
from app.bot.start import MENU_TEXT
from app.db import Repo, User

router = Router()


class RenameState(StatesGroup):
    waiting_label = State()


async def _usage_or_error(http, repo, acc):
    if acc.needs_relogin:
        return None, "нужен повторный вход"
    try:
        return await usage.fetch_account_usage(http, repo, acc), None
    except Exception as e:
        return None, str(e)[:200]


@router.callback_query(MenuCb.filter(F.action == "home"))
async def home(cq: CallbackQuery, user: User, state: FSMContext):
    await state.clear()
    await cq.message.edit_text(MENU_TEXT, reply_markup=keyboards.main_menu(user.is_admin))
    await cq.answer()


@router.callback_query(MenuCb.filter(F.action == "status"))
async def status(cq: CallbackQuery, user: User, repo: Repo, http):
    await cq.answer("Проверяю…")
    await cq.message.edit_text("⏳ <i>Проверяю лимиты аккаунтов…</i>")
    accounts = await repo.list_accounts(user.tg_id)
    results = await asyncio.gather(*(_usage_or_error(http, repo, a) for a in accounts))
    items = [(a.label, u, e) for a, (u, e) in zip(accounts, results)]
    await cq.message.edit_text(report.format_status(items), reply_markup=keyboards.back_home())


@router.callback_query(MenuCb.filter(F.action == "accounts"))
async def accounts(cq: CallbackQuery, user: User, repo: Repo):
    accs = await repo.list_accounts(user.tg_id)
    text = "🗂 <b>Ваши аккаунты</b>" if accs else "🗂 Аккаунтов пока нет."
    await cq.message.edit_text(text, reply_markup=keyboards.accounts_list(accs))
    await cq.answer()


async def show_card(message: Message, repo: Repo, http, user: User, acc_id: int, edit: bool = True):
    acc = await repo.get_account(acc_id, owner=user.tg_id)
    if not acc:
        return
    u, e = await _usage_or_error(http, repo, acc)
    acc = await repo.get_account(acc_id, owner=user.tg_id)  # мог стать needs_relogin
    text = report.format_account(acc.label, u, e)
    if acc.email:
        text += f"\n\n📧 {escape(acc.email)}"
    send = message.edit_text if edit else message.answer
    await send(text, reply_markup=keyboards.account_card(acc))


@router.callback_query(AccCb.filter(F.action == "open"))
async def open_card(cq: CallbackQuery, callback_data: AccCb, user: User, repo: Repo, http):
    await cq.answer()
    await show_card(cq.message, repo, http, user, callback_data.id)


@router.callback_query(AccCb.filter(F.action == "notify"))
async def notify(cq: CallbackQuery, callback_data: AccCb, user: User, repo: Repo, http):
    val = await repo.toggle_notify(callback_data.id, user.tg_id)
    await cq.answer("🔔 Уведомления включены" if val else "🔕 Уведомления выключены")
    await show_card(cq.message, repo, http, user, callback_data.id)


@router.callback_query(AccCb.filter(F.action == "delete"))
async def delete(cq: CallbackQuery, callback_data: AccCb):
    await cq.message.edit_text("🗑 Удалить аккаунт и его токены?",
                               reply_markup=keyboards.confirm_delete(callback_data.id))
    await cq.answer()


@router.callback_query(AccCb.filter(F.action == "confirm_delete"))
async def confirm_delete(cq: CallbackQuery, callback_data: AccCb, user: User, repo: Repo):
    await repo.delete_account(callback_data.id, user.tg_id)
    await cq.answer("Удалено")
    accs = await repo.list_accounts(user.tg_id)
    await cq.message.edit_text("🗂 <b>Ваши аккаунты</b>", reply_markup=keyboards.accounts_list(accs))


@router.callback_query(AccCb.filter(F.action == "rename"))
async def rename(cq: CallbackQuery, callback_data: AccCb, state: FSMContext):
    await state.set_state(RenameState.waiting_label)
    await state.update_data(acc_id=callback_data.id)
    await cq.message.answer("✏️ Пришлите новое название (до 64 символов).")
    await cq.answer()


@router.message(RenameState.waiting_label, F.text)
async def rename_done(message: Message, state: FSMContext, user: User, repo: Repo, http):
    data = await state.get_data()
    await state.clear()
    label = message.text.strip()[:64]
    if label:
        await repo.rename_account(data["acc_id"], user.tg_id, label)
    await show_card(message, repo, http, user, data["acc_id"], edit=False)
```

- [ ] **Step 2: `app/bot/add_account.py`**

```python
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
    retry = keyboards.InlineKeyboardMarkup(inline_keyboard=[[
        keyboards._btn("🔁 Начать заново", MenuCb(action="add"))]])
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
```

- [ ] **Step 3: `app/bot/admin.py`**

```python
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Filter
from aiogram.types import CallbackQuery

from app.bot import keyboards
from app.bot.callbacks import AdmCb, MenuCb
from app.config import Settings
from app.db import Repo, User

router = Router()


class IsAdmin(Filter):
    async def __call__(self, event, user: User | None = None) -> bool:
        return bool(user and user.is_admin)


router.callback_query.filter(IsAdmin())


@router.callback_query(MenuCb.filter(F.action == "access"))
async def access(cq: CallbackQuery):
    await cq.message.edit_text("👥 <b>Управление доступом</b>", reply_markup=keyboards.admin_menu())
    await cq.answer()


@router.callback_query(AdmCb.filter(F.action == "invite"))
async def invite(cq: CallbackQuery, repo: Repo, user: User, settings: Settings, bot: Bot):
    token = await repo.create_invite(user.tg_id, settings.invite_ttl)
    me = await bot.me()
    hours = settings.invite_ttl // 3600
    await cq.message.answer(
        f"🎟 Одноразовый инвайт (действует {hours} ч):\n"
        f"<code>https://t.me/{me.username}?start=inv_{token}</code>")
    await cq.answer()


@router.callback_query(AdmCb.filter(F.action == "users"))
async def users(cq: CallbackQuery, repo: Repo):
    lst = await repo.list_users()
    lines = ["👥 <b>Пользователи</b>"]
    for u in lst:
        name = f"@{escape(u.username)}" if u.username else str(u.tg_id)
        tag = "👑" if u.is_admin else ("🚫" if u.blocked else "✅")
        n = len(await repo.list_accounts(u.tg_id))
        lines.append(f"{tag} {name} — акк.: {n}")
    await cq.message.edit_text("\n".join(lines), reply_markup=keyboards.users_list(lst))
    await cq.answer()


@router.callback_query(AdmCb.filter(F.action == "revoke"))
async def revoke(cq: CallbackQuery, callback_data: AdmCb):
    await cq.message.edit_text(
        f"🚫 Отозвать доступ у {callback_data.tg_id}? Его аккаунты и токены будут удалены.",
        reply_markup=keyboards.confirm_revoke(callback_data.tg_id))
    await cq.answer()


@router.callback_query(AdmCb.filter(F.action == "confirm_revoke"))
async def confirm_revoke(cq: CallbackQuery, callback_data: AdmCb, repo: Repo):
    ok = await repo.block_user(callback_data.tg_id)
    await cq.answer("Доступ отозван" if ok else "Нельзя отозвать")
    await users(cq, repo)


@router.callback_query(AdmCb.filter(F.action == "diag"))
async def diag(cq: CallbackQuery, repo: Repo):
    s = await repo.stats()
    lines = [
        "🩺 <b>Диагностика</b>",
        f"Пользователей: {s['users']}",
        f"Аккаунтов: {s['accounts']} (требуют входа: {s['needs_relogin']})",
    ]
    if s["errors"]:
        lines.append("\n<b>Последние ошибки:</b>")
        lines += [f"• {escape(label)}: <i>{escape(err)}</i>" for label, err in s["errors"][:20]]
    await cq.message.edit_text("\n".join(lines), reply_markup=keyboards.admin_menu())
    await cq.answer()
```

- [ ] **Step 4: `app/main.py`**

```python
import asyncio
import logging

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.bot import add_account, admin, menu, start
from app.bot.access import AccessMiddleware
from app.config import load_settings
from app.crypto import Cipher
from app.db import Repo
from app.monitor import run_monitor


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    repo = await Repo.open(settings.db_path, Cipher(settings.encryption_key))
    await repo.upsert_admin(settings.admin_id)

    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    http = aiohttp.ClientSession()
    dp["repo"], dp["http"], dp["settings"] = repo, http, settings

    access = AccessMiddleware(repo)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    # add_account раньше menu: его FSM-хендлер кода должен ловить текст первым
    dp.include_routers(start.router, admin.router, add_account.router, menu.router)

    await bot.delete_webhook(drop_pending_updates=True)
    monitor = asyncio.create_task(run_monitor(bot, repo, http, settings.check_interval))
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        monitor.cancel()
        await http.close()
        await repo.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 5: Полный прогон тестов и импорт**

Run: `.venv/bin/pytest -v && .venv/bin/python -c "import app.main"`
Expected: все тесты зелёные, импорт без ошибок.

- [ ] **Step 6: Smoke-тест с реальным ботом (вместе с пользователем)**

```bash
ENCRYPTION_KEY=$(.venv/bin/python -m app.crypto) BOT_TOKEN=<тестовый> ADMIN_TELEGRAM_ID=<id> DB_PATH=./dev.db .venv/bin/python -m app.main
```
Проверить: `/start` от админа → меню; «Доступ → Создать инвайт» → ссылка; переход по ней со второго аккаунта Telegram → доступ; сообщение от третьего аккаунта без инвайта → тишина; «Добавить аккаунт» → вход → вставка кода (сообщение удаляется) → карточка с лимитами; переименование, вкл/выкл уведомлений, удаление; «Отозвать» → второй пользователь больше не получает ответов; «Статус».

- [ ] **Step 7: Commit**

```bash
git add app
git commit -m "feat: inline-меню, добавление аккаунтов через OAuth и админка"
```

---

### Task 8: Развёртывание, удаление legacy, README

**Files:**
- Modify: `Dockerfile`, `docker-compose.yml`, `README.md`, `.gitignore`
- Delete: `claude_checker.py`, `config.example.json`

- [ ] **Step 1: Dockerfile**

```dockerfile
FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 DB_PATH=/data/bot.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

VOLUME /data

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sqlite3,os; sqlite3.connect(os.environ['DB_PATH']).execute('select 1')"

CMD ["python", "-m", "app.main"]
```

- [ ] **Step 2: docker-compose.yml**

```yaml
services:
  claude-limits-checker:
    build: .
    container_name: claude-limits-checker
    restart: unless-stopped
    environment:
      - BOT_TOKEN=${BOT_TOKEN}
      - ADMIN_TELEGRAM_ID=${ADMIN_TELEGRAM_ID}
      # Сгенерировать: python -m app.crypto. Потеря ключа = переподключение всех аккаунтов.
      - ENCRYPTION_KEY=${ENCRYPTION_KEY}
      - CHECK_INTERVAL=300
    volumes:
      - claude_checker_data:/data

volumes:
  claude_checker_data:
```

- [ ] **Step 3: Удалить legacy и почистить .gitignore**

```bash
git rm claude_checker.py config.example.json
```
В `.gitignore` убрать строки `config.export.json`, `state.json`, `state.json.tmp`; оставить `config.json` (локальный файл пользователя), `.env*`, добавленные ранее `.venv/`, `*.db`.

- [ ] **Step 4: README**

Переписать разделы: возможности (мультипользовательский режим, инвайты, меню, OAuth-вход), переменные окружения (таблица из Global Constraints), генерация `ENCRYPTION_KEY` (`python -m app.crypto`) с предупреждением о потере ключа, развёртывание в Coolify (Dockerfile + persistent storage на `/data`), первый запуск (админ пишет `/start`), как пригласить пользователя, как добавить аккаунт, локальная разработка (`pip install -r requirements-dev.txt`, `pytest`). Удалить всё про Keychain, `export`, `CONFIG_JSON`, `STATE_FILE`, `claude auth login`.

- [ ] **Step 5: Проверка сборки**

Run: `docker build -t claude-limits-checker . && .venv/bin/pytest -q`
Expected: образ собирается, тесты зелёные.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore: Docker под новый бот, удалён однопользовательский скрипт, README"
```
