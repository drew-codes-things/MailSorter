import hmac
import secrets
import time
from dataclasses import dataclass

from .db import Database
from .google import TokenGrant
from .secrets_store import TokenVault, digest

SESSION_SECONDS = 30 * 24 * 3600
SIGN_IN_SECONDS = 10 * 60
SESSION_ID_BYTES = 32


@dataclass(frozen=True)
class StoredTokens:
    email: str
    access_token: str
    access_expires_at: float
    refresh_token: str


@dataclass(frozen=True)
class LastAction:
    message_id: str
    action: str
    had_later: bool
    done_at: float


class AccountStore:
    def __init__(self, database: Database, vault: TokenVault, clock=time.time):
        self.database = database
        self.vault = vault
        self.clock = clock

    def save_sign_in(self, email: str, grant: TokenGrant) -> None:
        now = self.clock()
        with self.database.connect(immediate=True) as connection:
            connection.execute(
                "INSERT INTO accounts (email, refresh_token, access_token, access_expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(email) DO UPDATE SET refresh_token = excluded.refresh_token, "
                "access_token = excluded.access_token, access_expires_at = excluded.access_expires_at, "
                "updated_at = excluded.updated_at",
                (email, self.vault.seal(grant.refresh_token or ""), self.vault.seal(grant.access_token), grant.expires_at,
                 now, now),
            )

    def tokens(self, email: str) -> StoredTokens | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT refresh_token, access_token, access_expires_at FROM accounts WHERE email = ?", (email,)
            ).fetchone()
        if row is None:
            return None
        return StoredTokens(email, self.vault.open(row["access_token"]), row["access_expires_at"],
                            self.vault.open(row["refresh_token"]))

    def save_access(self, email: str, grant: TokenGrant) -> None:
        with self.database.connect(immediate=True) as connection:
            connection.execute(
                "UPDATE accounts SET access_token = ?, access_expires_at = ?, updated_at = ? WHERE email = ?",
                (self.vault.seal(grant.access_token), grant.expires_at, self.clock(), email),
            )
            if grant.refresh_token:
                connection.execute("UPDATE accounts SET refresh_token = ? WHERE email = ?",
                                   (self.vault.seal(grant.refresh_token), email))

    def forget_account(self, email: str) -> None:
        with self.database.connect(immediate=True) as connection:
            connection.execute("DELETE FROM accounts WHERE email = ?", (email,))

    def forget_account_if_refresh_token_is(self, email: str, refused_refresh_token: str) -> bool:
        with self.database.connect(immediate=True) as connection:
            row = connection.execute("SELECT refresh_token FROM accounts WHERE email = ?", (email,)).fetchone()
            if row is None:
                return True
            if not hmac.compare_digest(self.vault.open(row["refresh_token"]), refused_refresh_token):
                return False
            connection.execute("DELETE FROM accounts WHERE email = ?", (email,))
        return True

    def forget_accounts_except(self, allowed: tuple[str, ...]) -> int:
        placeholders = ", ".join("?" for _ in allowed)
        with self.database.connect(immediate=True) as connection:
            removed = connection.execute(f"DELETE FROM accounts WHERE email NOT IN ({placeholders})", allowed).rowcount
        return removed

    def start_session(self, email: str) -> str:
        session_id = secrets.token_urlsafe(SESSION_ID_BYTES)
        now = self.clock()
        with self.database.connect(immediate=True) as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute("INSERT INTO sessions (id_hash, email, created_at, expires_at) VALUES (?, ?, ?, ?)",
                               (digest(session_id), email, now, now + SESSION_SECONDS))
        return session_id

    def session_email(self, session_id: str | None) -> str | None:
        if not session_id or len(session_id) > 200:
            return None
        with self.database.connect() as connection:
            row = connection.execute("SELECT email, expires_at FROM sessions WHERE id_hash = ?",
                                     (digest(session_id),)).fetchone()
        if row is None or row["expires_at"] <= self.clock():
            return None
        return row["email"]

    def end_session(self, session_id: str) -> None:
        with self.database.connect(immediate=True) as connection:
            connection.execute("DELETE FROM sessions WHERE id_hash = ?", (digest(session_id),))

    def record_action(self, email: str, message_id: str, action: str, had_later: bool) -> None:
        with self.database.connect(immediate=True) as connection:
            connection.execute(
                "INSERT INTO last_actions (email, message_id, action, had_later, done_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET message_id = excluded.message_id, action = excluded.action, "
                "had_later = excluded.had_later, done_at = excluded.done_at",
                (email, message_id, action, int(had_later), self.clock()),
            )

    def last_action(self, email: str) -> LastAction | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT message_id, action, had_later, done_at FROM last_actions WHERE email = ?",
                                     (email,)).fetchone()
        if row is None:
            return None
        return LastAction(row["message_id"], row["action"], bool(row["had_later"]), row["done_at"])

    def clear_last_action(self, email: str) -> None:
        with self.database.connect(immediate=True) as connection:
            connection.execute("DELETE FROM last_actions WHERE email = ?", (email,))
