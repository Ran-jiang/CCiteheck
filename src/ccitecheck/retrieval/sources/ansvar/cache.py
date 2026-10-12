"""Ansvar get_provision 的最小 SQLite 缓存。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Optional

from ....infrastructure.paths import PROJECT_ROOT
from .client import AnsvarMcpClient

DEFAULT_CACHE_DB = PROJECT_ROOT / "data" / "ansvar_cache.sqlite"
TTL_SECONDS = 7 * 86400


def cache_enabled() -> bool:
    return os.getenv("ANSVAR_CACHE_DISABLE", "") != "1"


def cache_db_path() -> Path:
    return Path(os.getenv("ANSVAR_CACHE_DB") or DEFAULT_CACHE_DB)


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    db_path = Path(path) if path else cache_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provision_cache (
            key TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            payload TEXT NOT NULL,
            verified_at INTEGER NOT NULL
        )
        """
    )
    return conn


@dataclass
class CachedAnsvarClient:
    client: AnsvarMcpClient
    db_path: Optional[Path] = None
    # ponytail: one lock prevents duplicate quota spend; split per key if contention matters.
    _miss_lock: object = field(default_factory=Lock, init=False, repr=False)

    def search_law(self, query: str, jurisdiction: str = ""):
        return self.client.search_law(query, jurisdiction=jurisdiction)

    def get_article_text(
        self,
        identifier: str,
        article_number: str,
        *,
        jurisdiction: str = "",
        lookup_arguments: Optional[dict] = None,
    ) -> Optional[dict]:
        key = json.dumps(
            [
                jurisdiction.upper(),
                identifier.strip(),
                article_number.strip(),
                lookup_arguments or {},
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with self._miss_lock:
            with _connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT status, payload, verified_at FROM provision_cache WHERE key = ?",
                    (key,),
                ).fetchone()
                now = int(time.time())
                if row is not None and now - row["verified_at"] <= TTL_SECONDS:
                    return None if row["status"] == "not_found" else json.loads(row["payload"])
                if row is not None:
                    conn.execute("DELETE FROM provision_cache WHERE key = ?", (key,))
                result = self.client.get_article_text(
                    identifier,
                    article_number,
                    jurisdiction=jurisdiction,
                    lookup_arguments=lookup_arguments,
                )
                now = int(time.time())
                conn.execute(
                    "INSERT OR REPLACE INTO provision_cache (key, status, payload, verified_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        key,
                        "found" if result is not None else "not_found",
                        json.dumps(result or {}, ensure_ascii=False),
                        now,
                    ),
                )
                conn.commit()
                return result


__all__ = ["CachedAnsvarClient", "cache_enabled"]
