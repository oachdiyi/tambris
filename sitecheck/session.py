"""SQLite-backed session store: checkpoints, dedup state, pending queue, results.

The database is the single source of truth for recovery. All writes are
batched/transactional; results are committed incrementally so a crash never
loses classified work.
"""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Dict, Iterable, List, Optional, Tuple

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS seen_urls (
    url TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS pending_urls (
    url TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS results (
    url TEXT PRIMARY KEY,
    final_url TEXT,
    status TEXT NOT NULL,
    category TEXT NOT NULL,
    status_code INTEGER,
    reason TEXT,
    proxy TEXT,
    ts REAL NOT NULL
);
"""


class SessionDB:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    # ----- meta / checkpoint -----

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_int(self, key: str, default: int = 0) -> int:
        raw = self.get_meta(key)
        return int(raw) if raw is not None else default

    def checkpoint(self, byte_offset: int, settings_json: str) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO meta(key,value) VALUES('byte_offset',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(byte_offset),),
        )
        cur.execute(
            "INSERT INTO meta(key,value) VALUES('settings',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (settings_json,),
        )
        cur.execute(
            "INSERT INTO meta(key,value) VALUES('checkpoint_ts',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(time.time()),),
        )
        self.conn.commit()

    # ----- dedup -----

    def mark_seen(self, urls: Iterable[str]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_urls(url) VALUES(?)",
            ((u,) for u in urls),
        )

    def seen_contains(self, url: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM seen_urls WHERE url=? LIMIT 1", (url,)
            ).fetchone()
            is not None
        )

    # ----- pending queue -----

    def add_pending(self, urls: Iterable[str]) -> None:
        self.conn.executemany(
            "INSERT OR IGNORE INTO pending_urls(url) VALUES(?)",
            ((u,) for u in urls),
        )

    def pop_pending(self, limit: int) -> List[str]:
        rows = self.conn.execute(
            "SELECT url FROM pending_urls LIMIT ?", (limit,)
        ).fetchall()
        if rows:
            self.conn.executemany(
                "DELETE FROM pending_urls WHERE url=?", ((r[0],) for r in rows)
            )
            self.conn.commit()
        return [r[0] for r in rows]

    def pending_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM pending_urls").fetchone()[0]

    # ----- results -----

    def save_result(
        self,
        url: str,
        final_url: Optional[str],
        status: str,
        category: str,
        status_code: Optional[int],
        reason: str,
        proxy: Optional[str],
        ts: Optional[float] = None,
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO results"
            "(url,final_url,status,category,status_code,reason,proxy,ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                url,
                final_url,
                status,
                category,
                status_code,
                reason,
                proxy,
                ts or time.time(),
            ),
        )

    def commit(self) -> None:
        self.conn.commit()

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for status, n in self.conn.execute(
            "SELECT category, COUNT(*) FROM results GROUP BY category"
        ):
            out[status] = n
        return out

    def all_results(self) -> List[Tuple]:
        return self.conn.execute(
            "SELECT url,final_url,status,category,status_code,reason,proxy,ts "
            "FROM results"
        ).fetchall()
