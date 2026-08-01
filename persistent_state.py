# persistent_state.py
import json
import os
import sqlite3
from contextlib import closing

# ─────────────────────────────────────────────────────────────
# Drop-in, dict-like state that survives restarts (SQLite-backed) instead of living
# only in memory. conversation_history and last_personality used to be plain {} dicts
# rebuilt from scratch on every deploy — every restart wiped every guild's conversation
# state. These classes support the same .get()/.setdefault()/[]/in usage the rest of
# the codebase already relies on, so no call sites elsewhere need to change.
DB_PATH = os.getenv("STATE_DB_PATH", "data/state.db")


def _connect() -> sqlite3.Connection:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    return sqlite3.connect(DB_PATH)


class PersistentDict:
    """A dict-like store backed by SQLite. Values must be JSON-serializable."""

    def __init__(self, table: str):
        self._table = table
        with closing(_connect()) as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            conn.commit()

    def _read(self, key):
        with closing(_connect()) as conn:
            row = conn.execute(f"SELECT value FROM {self._table} WHERE key = ?", (str(key),)).fetchone()
        return json.loads(row[0]) if row else None

    def _write(self, key, value) -> None:
        with closing(_connect()) as conn:
            conn.execute(
                f"INSERT INTO {self._table} (key, value) VALUES (?, ?) "
                f"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), json.dumps(value)),
            )
            conn.commit()

    def get(self, key, default=None):
        value = self._read(key)
        return default if value is None else value

    def setdefault(self, key, default):
        value = self._read(key)
        if value is None:
            self._write(key, default)
            return default
        return value

    def __getitem__(self, key):
        value = self._read(key)
        if value is None:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value) -> None:
        self._write(key, value)

    def __contains__(self, key) -> bool:
        return self._read(key) is not None


class _PersistentList(list):
    """A list whose in-place mutations (append/insert/item assignment) write the full
    updated list back to its PersistentListDict, so callers can keep treating it like
    a plain in-memory list (history.append(...), history[0] = ..., etc.)."""

    def __init__(self, store: "PersistentListDict", key, items):
        super().__init__(items)
        self._store = store
        self._key = key

    def append(self, item) -> None:
        super().append(item)
        self._store._write(self._key, list(self))

    def insert(self, index, item) -> None:
        super().insert(index, item)
        self._store._write(self._key, list(self))

    def __setitem__(self, index, value) -> None:
        super().__setitem__(index, value)
        self._store._write(self._key, list(self))


class PersistentListDict(PersistentDict):
    """Like PersistentDict, but values are lists that support write-through in-place
    mutation (used for conversation_history)."""

    def get(self, key, default=None):
        value = self._read(key)
        if value is None:
            return default
        return _PersistentList(self, key, value)

    def setdefault(self, key, default):
        value = self._read(key)
        if value is None:
            self._write(key, default)
            value = default
        return _PersistentList(self, key, value)

    def __getitem__(self, key):
        value = self._read(key)
        if value is None:
            raise KeyError(key)
        return _PersistentList(self, key, value)
