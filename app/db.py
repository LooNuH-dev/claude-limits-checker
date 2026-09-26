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
