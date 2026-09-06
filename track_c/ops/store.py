"""One writer plus atomic state/event commits; SQLite retains uncertainty across restart."""
import json
from pathlib import Path
import sqlite3
import threading
import time


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.guard = sqlite3.connect(self.directory / "writer.lock.sqlite", timeout=0)
        try:
            self.guard.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            self.guard.close()
            raise RuntimeError("another Track C writer is running") from None
        self.mutex = threading.RLock()
        self.db = sqlite3.connect(self.directory / "ledger.sqlite", timeout=5, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);"
                              "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, t_ms INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL);")

    def load(self):
        row = self.db.execute("SELECT body FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def save(self, state, kind, **fields):
        body = encoded(state)
        with self.mutex, self.db:
            self.db.execute("INSERT INTO state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (body,))
            self.db.execute("INSERT INTO events(t_ms,kind,body) VALUES(?,?,?)", (int(time.time()*1000), kind, encoded(fields)))

    def event(self, kind, **fields):
        with self.mutex, self.db:
            self.db.execute("INSERT INTO events(t_ms,kind,body) VALUES(?,?,?)", (int(time.time()*1000), kind, encoded(fields)))

    def close(self):
        self.db.close()
        self.guard.close()
