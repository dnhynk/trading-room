"""Durable attempt registry. Registration precedes evaluation, including failures."""
import datetime as dt
import json
from pathlib import Path
import sqlite3

from .config import canonical, digest
from .data import epoch_ms


def now_ms():
    return int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)


class Registry:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS trials(
                id TEXT PRIMARY KEY, created INTEGER NOT NULL,
                specification TEXT NOT NULL, status TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS results(
                trial TEXT PRIMARY KEY REFERENCES trials(id),
                completed INTEGER NOT NULL, result TEXT NOT NULL);
        """)

    def register(self, config, sources, window=None, prospective=False, created=None):
        created = now_ms() if created is None else created
        if prospective and (not window or epoch_ms(window["start"]) <= created or epoch_ms(window["end"]) <= epoch_ms(window["start"])):
            raise ValueError("prospective window must be registered before it starts")
        spec = dict(config=config.data, sources=sources, window=window, prospective=prospective)
        identifier = digest(dict(spec, registered_at=created))
        with self.db:
            # Serialize overlap-check + registration, including separate processes.
            self.db.execute("BEGIN IMMEDIATE")
            if prospective:
                for (previous,) in self.db.execute("SELECT specification FROM trials"):
                    previous = json.loads(previous)
                    old = previous.get("window")
                    if previous.get("prospective") and max(epoch_ms(old["start"]), epoch_ms(window["start"])) < min(epoch_ms(old["end"]), epoch_ms(window["end"])):
                        raise ValueError("prospective window overlaps an already registered trial")
            self.db.execute("INSERT INTO trials VALUES(?,?,?,?)", (identifier, created, canonical(spec), "registered"))
        return identifier

    def claim(self, identifier, config, source_code):
        with self.db:
            row = self.db.execute("SELECT specification,status FROM trials WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise ValueError("unknown trial")
            spec = json.loads(row[0])
            if row[1] != "registered" or canonical(spec["config"]) != config.text or spec["sources"]["code"] != source_code:
                raise ValueError("trial already consumed, configuration or code changed")
            changed = self.db.execute("UPDATE trials SET status='running' WHERE id=? AND status='registered'", (identifier,)).rowcount
            if changed != 1:
                raise ValueError("trial claimed concurrently")
        return spec

    def finish(self, identifier, result, failed=False):
        with self.db:
            row = self.db.execute("SELECT status FROM trials WHERE id=?", (identifier,)).fetchone()
            if not row or row[0] != "running":
                raise ValueError("only a running trial may finish")
            self.db.execute("INSERT INTO results VALUES(?,?,?)", (identifier, now_ms(), canonical(result)))
            self.db.execute("UPDATE trials SET status=? WHERE id=?", ("failed" if failed else "finished", identifier))

    def list(self):
        return [dict(id=r[0], created=r[1], status=r[2]) for r in self.db.execute("SELECT id,created,status FROM trials ORDER BY created,id")]

    def close(self):
        self.db.close()
