import sqlite3
from contextlib import contextmanager
from pathlib import Path

BUSY_TIMEOUT_SECONDS = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    email TEXT PRIMARY KEY,
    refresh_token BLOB NOT NULL,
    access_token BLOB NOT NULL,
    access_expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL REFERENCES accounts(email) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_by_email ON sessions (email);
CREATE TABLE IF NOT EXISTS last_actions (
    email TEXT PRIMARY KEY REFERENCES accounts(email) ON DELETE CASCADE,
    message_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('yes', 'no', 'later')),
    had_later INTEGER NOT NULL,
    done_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

    def _open(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connect(self, immediate: bool = False):
        connection = self._open()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def is_reachable(self) -> bool:
        try:
            with self.connect() as connection:
                connection.execute("SELECT COUNT(*) FROM accounts").fetchone()
        except sqlite3.Error:
            return False
        return True
