"""SQLite-backed cache: HTTP responses and per-file analysis results.

Keeps re-runs cheap and makes the whole pipeline resumable, which matters
when a run over a thousand files is interrupted half way.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS http (
    key TEXT PRIMARY KEY, body TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS doc (
    crc TEXT PRIMARY KEY, payload TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS decision (
    key TEXT PRIMARY KEY, value TEXT NOT NULL, ts REAL NOT NULL);
"""


class Cache:
    def __init__(self, path: Path, ttl_days: int = 90) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_days * 86400
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -------------------------------------------------------------- generic
    def _get(self, table: str, col: str, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {col}, ts FROM {table} WHERE key=?" if table != "doc"
                else "SELECT payload, ts FROM doc WHERE crc=?", (key,)
            ).fetchone()
        if not row:
            return None
        body, ts = row
        if self.ttl and time.time() - ts > self.ttl:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    def _put(self, table: str, key: str, value: Any) -> None:
        blob = json.dumps(value, ensure_ascii=False)
        with self._lock:
            if table == "doc":
                self._conn.execute(
                    "INSERT OR REPLACE INTO doc(crc,payload,ts) VALUES(?,?,?)",
                    (key, blob, time.time()))
            else:
                col = "body" if table == "http" else "value"
                self._conn.execute(
                    f"INSERT OR REPLACE INTO {table}(key,{col},ts) VALUES(?,?,?)",
                    (key, blob, time.time()))
            self._conn.commit()

    # ------------------------------------------------------------- accessors
    def get_http(self, key: str) -> Any | None:
        return self._get("http", "body", key)

    def put_http(self, key: str, value: Any) -> None:
        self._put("http", key, value)

    def iter_http(self):
        """Every cached HTTP answer, as (key, body); for building a lexicon
        of what the registries have said so far."""
        with self._lock:
            rows = self._conn.execute("SELECT key, body FROM http").fetchall()
        for key, body in rows:
            try:
                yield key, json.loads(body)
            except json.JSONDecodeError:
                continue

    def get_doc(self, crc: str) -> dict | None:
        return self._get("doc", "payload", crc)

    def put_doc(self, crc: str, value: dict) -> None:
        self._put("doc", crc, value)

    def get_decision(self, key: str) -> Any | None:
        return self._get("decision", "value", key)

    def put_decision(self, key: str, value: Any) -> None:
        self._put("decision", key, value)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class NullCache(Cache):
    """Drop-in cache that stores nothing (``--no-cache``)."""

    def __init__(self) -> None:
        self.ttl = 0

    def get_http(self, key): return None
    def iter_http(self): return iter(())
    def put_http(self, key, value): pass
    def get_doc(self, crc): return None
    def put_doc(self, crc, value): pass
    def get_decision(self, key): return None
    def put_decision(self, key, value): pass
    def close(self): pass
