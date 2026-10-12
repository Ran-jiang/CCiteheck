"""北大法宝查询结果的本地 SQLite 缓存。

缓存策略（与产品约定一致）：
- 现行有效：缓存完整负载（含条文全文与溯源链接），7 天后不直接失效，
  而是做一次轻量"时效再验证"（只查法规列表元数据）：时效未变则续期，
  变了才作废重查，把额度消耗降到最低。
- 废止或失效：同样缓存完整元数据，30 天。
- 未找到：缓存否定结论 7 天，避免同一部虚构/错名法规反复打接口。

缓存管理函数通过本模块的 Python API 暴露。
缓存库默认在 data/pkulaw_cache.sqlite（已 gitignore），可用环境变量
PKULAW_CACHE_DB 覆盖路径、PKULAW_CACHE_DISABLE=1 整体停用。
"""

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
from ....infrastructure.database import normalize_article_key
from ....query_construction.strategies.common import build_article_semantic_fallback_query
from .client import (
    PkulawArticle,
    PkulawLawRecord,
    PkulawMcpClient,
    PkulawNotFoundError,
    PkulawMcpError,
    PkulawRecognizedLaw,
)
from ....query_construction.matching import (
    match_law_record,
    matches_requested_law_title,
)
from .urls import usable_mcp_url

DEFAULT_CACHE_DB = PROJECT_ROOT / "data" / "pkulaw_cache.sqlite"

TTL_SECONDS = {
    "effective": 7 * 86400,
    "repealed": 30 * 86400,
    "not_found": 7 * 86400,
}


def _article_key(title: str, article_no: str) -> str:
    return f"{title}|{normalize_article_key(article_no)}"


def _now() -> int:
    return int(time.time())


def _is_repealed(timeliness: list[str]) -> bool:
    return any(
        any(token in value for token in (
            "废止", "失效", "已被修改", "已修改", "部分失效"
        ))
        for value in timeliness
    )


def _article_matches(title: str, article_no: str, article: PkulawArticle) -> bool:
    return bool(
        matches_requested_law_title(title, article.title)
        and normalize_article_key(article_no) == normalize_article_key(article.article_no)
    )


def cache_db_path() -> Path:
    return Path(os.getenv("PKULAW_CACHE_DB") or DEFAULT_CACHE_DB)


def cache_enabled() -> bool:
    return os.getenv("PKULAW_CACHE_DISABLE", "") != "1"


def connect_cache(path: Optional[Path] = None) -> sqlite3.Connection:
    db_path = Path(path) if path else cache_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cache_entries (
            kind TEXT NOT NULL,            -- 'article' | 'law'
            key TEXT NOT NULL,             -- article: 法名|条号；law: 法名
            status TEXT NOT NULL,          -- 'effective' | 'repealed' | 'not_found'
            payload TEXT NOT NULL,         -- JSON；not_found 仅存最小元数据
            fetched_at INTEGER NOT NULL,   -- 首次抓取时间
            verified_at INTEGER NOT NULL,  -- 最近一次时效验证时间
            PRIMARY KEY (kind, key)
        )
        """
    )
    return conn


@dataclass
class CachedPkulawClient:
    """在真实 MCP 客户端外面包一层缓存；接口与 PkulawMcpClient 一致。"""

    client: PkulawMcpClient
    db_path: Optional[Path] = None
    _miss_lock: object = field(default_factory=Lock, init=False, repr=False)

    # ---- 条文全文 ----

    def get_article(self, title: str, article_no: str) -> PkulawArticle:
        key = _article_key(title, article_no)
        with connect_cache(self.db_path) as conn:
            entry = self._fresh_entry(conn, "article", key)
            if entry is not None:
                if entry["status"] == "not_found":
                    raise PkulawNotFoundError("未找到数据（缓存）")
                cached = _article_from_payload(json.loads(entry["payload"]))
                if _article_matches(title, article_no, cached):
                    return cached
                conn.execute(
                    "DELETE FROM cache_entries WHERE kind = 'article' AND key = ?",
                    (key,),
                )
                conn.commit()
            try:
                article = self.client.get_article(title, article_no)
            except PkulawNotFoundError:
                _upsert(conn, "article", key, "not_found", {"title": title})
                raise
            if not _article_matches(title, article_no, article):
                raise PkulawMcpError("北大法宝精确条文返回的法名或条号不匹配")
            _upsert(
                conn,
                "article",
                key,
                "repealed" if _is_repealed(article.timeliness) else "effective",
                _article_payload(article),
            )
            return article

    def search_law_articles_for_article(
        self, title: str, article_no: str
    ) -> list[PkulawArticle]:
        key = _article_key(title, article_no)
        with connect_cache(self.db_path) as conn:
            entry = self._fresh_entry(conn, "article_semantic", key)
            if entry is not None:
                if entry["status"] == "not_found":
                    return []
                return [
                    _article_from_payload(item) for item in json.loads(entry["payload"])
                ]
            query = build_article_semantic_fallback_query(title, article_no)
            try:
                articles = self.client.search_law_articles(query)
            except PkulawNotFoundError:
                _upsert(conn, "article_semantic", key, "not_found", [])
                raise
            if articles:
                _upsert(
                    conn,
                    "article_semantic",
                    key,
                    "effective",
                    [_article_payload(article) for article in articles],
                )
            else:
                _upsert(conn, "article_semantic", key, "not_found", [])
            return articles

    # ---- 法规列表（含时效元数据） ----

    def get_law_list(
        self, title: str = "", fulltext: str = ""
    ) -> list[PkulawLawRecord]:
        if fulltext or not title:
            return self.client.get_law_list(title=title, fulltext=fulltext)
        with connect_cache(self.db_path) as conn:
            entry = self._fresh_entry(conn, "law", title)
            if entry is not None:
                if entry["status"] == "not_found":
                    raise PkulawNotFoundError("未找到数据（缓存）")
                payload = json.loads(entry["payload"])
                if entry["status"] != "repealed" or all(
                    "issue_date" in item for item in payload
                ):
                    return [_record_from_payload(item) for item in payload]
                # 兼容旧缓存：废止记录过去只保存法名和时效，立即回源补齐。
                conn.execute(
                    "DELETE FROM cache_entries WHERE kind = 'law' AND key = ?",
                    (title,),
                )
                conn.commit()

            try:
                records = self.client.get_law_list(title=title)
            except PkulawNotFoundError:
                _upsert(conn, "law", title, "not_found", {"title": title})
                raise
            matched = match_law_record(title, records)
            repealed = matched is not None and _is_repealed(matched.timeliness)
            payload = [_record_payload(record) for record in records]
            _upsert(
                conn, "law", title, "repealed" if repealed else "effective", payload
            )
            return records

    # ---- 法规识别及返回全文 ----

    def recognize_laws(self, text: str) -> list[PkulawRecognizedLaw]:
        with connect_cache(self.db_path) as conn:
            entry = self._fresh_entry(conn, "law_recognition", text)
            if entry is not None:
                if entry["status"] == "not_found":
                    return []
                return [
                    _recognized_from_payload(item)
                    for item in json.loads(entry["payload"])
                ]
            laws = self.client.recognize_laws(text)
            _upsert(
                conn,
                "law_recognition",
                text,
                "effective" if laws else "not_found",
                [_recognized_payload(law) for law in laws],
            )
            return laws

    # ---- 定位检索缓存；案例检索直接透传 ----

    def search_law_articles(self, text: str):
        with self._miss_lock:
            with connect_cache(self.db_path) as conn:
                entry = self._fresh_entry(conn, "law_semantic_query", text)
                if entry is not None:
                    if entry["status"] == "not_found":
                        return []
                    return [
                        _article_from_payload(item)
                        for item in json.loads(entry["payload"])
                    ]
                try:
                    articles = self.client.search_law_articles(text)
                except PkulawNotFoundError:
                    _upsert(conn, "law_semantic_query", text, "not_found", [])
                    raise
                _upsert(
                    conn,
                    "law_semantic_query",
                    text,
                    "effective" if articles else "not_found",
                    [_article_payload(article) for article in articles],
                )
                return articles

    def get_law_item_content(self, title: str, article_no: str):
        key = _article_key(title, article_no)
        with self._miss_lock:
            with connect_cache(self.db_path) as conn:
                entry = self._fresh_entry(conn, "law_item", key)
                if entry is not None:
                    if entry["status"] == "not_found":
                        raise PkulawNotFoundError("未找到数据（缓存）")
                    cached = _article_from_payload(json.loads(entry["payload"]))
                    if _article_matches(title, article_no, cached):
                        return cached
                    conn.execute(
                        "DELETE FROM cache_entries WHERE kind = 'law_item' AND key = ?",
                        (key,),
                    )
                    conn.commit()
                try:
                    article = self.client.get_law_item_content(title, article_no)
                except PkulawNotFoundError:
                    _upsert(conn, "law_item", key, "not_found", {"title": title})
                    raise
                if not _article_matches(title, article_no, article):
                    raise PkulawMcpError("北大法宝精确条文返回的法名或条号不匹配")
                _upsert(
                    conn,
                    "law_item",
                    key,
                    "repealed" if _is_repealed(article.timeliness) else "effective",
                    _article_payload(article),
                )
                return article

    def get_case_list(self, title: str = "", fulltext: str = ""):
        return self.client.get_case_list(title=title, fulltext=fulltext)

    def search_cases(self, text: str):
        return self.client.search_cases(text)

    # ---- 内部 ----

    def _fresh_entry(self, conn: sqlite3.Connection, kind: str, key: str):
        row = conn.execute(
            "SELECT * FROM cache_entries WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        if row is None:
            return None
        age = _now() - row["verified_at"]
        if age <= TTL_SECONDS[row["status"]]:
            return row
        if row["status"] == "effective" and row["kind"] != "law_semantic_query":
            # 过期不直接作废：轻量再验证时效，未变则续期
            if self._revalidate(conn, row):
                return conn.execute(
                    "SELECT * FROM cache_entries WHERE kind = ? AND key = ?",
                    (kind, key),
                ).fetchone()
        conn.execute(
            "DELETE FROM cache_entries WHERE kind = ? AND key = ?", (kind, key)
        )
        conn.commit()
        return None

    def _revalidate(self, conn: sqlite3.Connection, row) -> bool:
        title = row["key"].split("|", 1)[0]
        try:
            records = self.client.get_law_list(title=title)
        except PkulawMcpError:
            # 明确的法宝服务故障只短期续用旧数据，避免网络抖动触发缓存雪崩；
            # 其他异常不捕获，确保编程和解析错误及时暴露。
            conn.execute(
                "UPDATE cache_entries SET verified_at = ? WHERE kind = ? AND key = ?",
                (
                    _now() - TTL_SECONDS["effective"] + 86400,
                    row["kind"],
                    row["key"],
                ),
            )
            conn.commit()
            return True
        matched = match_law_record(title, records)
        if matched is not None and not _is_repealed(matched.timeliness):
            conn.execute(
                "UPDATE cache_entries SET verified_at = ? WHERE kind = ? AND key = ?",
                (_now(), row["kind"], row["key"]),
            )
            conn.commit()
            return True
        return False


# ---- 管理操作（供 CLI cache 命令使用） ----


def cache_status(db_path: Optional[Path] = None) -> dict:
    with connect_cache(db_path) as conn:
        rows = conn.execute(
            "SELECT kind, status, COUNT(*) AS n, MIN(fetched_at) AS oldest FROM cache_entries GROUP BY kind, status"
        ).fetchall()
        now = _now()
        expired = conn.execute(
            "SELECT COUNT(*) FROM cache_entries WHERE (? - verified_at) > "
            "CASE status WHEN 'effective' THEN ? WHEN 'repealed' THEN ? ELSE ? END",
            (
                now,
                TTL_SECONDS["effective"],
                TTL_SECONDS["repealed"],
                TTL_SECONDS["not_found"],
            ),
        ).fetchone()[0]
        return {
            "path": str(db_path or cache_db_path()),
            "groups": [dict(row) for row in rows],
            "expired": expired,
        }


def cache_refresh(
    client: Optional[PkulawMcpClient] = None, db_path: Optional[Path] = None
) -> dict:
    """再验证所有过期的现行有效条目；清除过期的否定/废止条目。"""
    real_client = client or PkulawMcpClient()
    cached = CachedPkulawClient(real_client, db_path)
    revalidated = removed = 0
    with connect_cache(db_path) as conn:
        now = _now()
        rows = conn.execute("SELECT * FROM cache_entries").fetchall()
        for row in rows:
            if (now - row["verified_at"]) <= TTL_SECONDS[row["status"]]:
                continue
            if row["status"] == "effective":
                if cached._revalidate(conn, row):
                    revalidated += 1
                    continue
            conn.execute(
                "DELETE FROM cache_entries WHERE kind = ? AND key = ?",
                (row["kind"], row["key"]),
            )
            removed += 1
        conn.commit()
    return {"revalidated": revalidated, "removed": removed}


def cache_clear(db_path: Optional[Path] = None) -> int:
    with connect_cache(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0]
        conn.execute("DELETE FROM cache_entries")
        conn.commit()
        return count


def _upsert(
    conn: sqlite3.Connection, kind: str, key: str, status: str, payload
) -> None:
    now = _now()
    conn.execute(
        "INSERT OR REPLACE INTO cache_entries (kind, key, status, payload, fetched_at, verified_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (kind, key, status, json.dumps(payload, ensure_ascii=False), now, now),
    )
    conn.commit()


def _record_payload(record: PkulawLawRecord) -> dict:
    return {
        "title": record.title,
        "url": record.url,
        "category": record.category,
        "document_no": record.document_no,
        "issue_department": record.issue_department,
        "issue_date": record.issue_date,
        "implement_date": record.implement_date,
        "timeliness": record.timeliness,
        "effectiveness": record.effectiveness,
    }


def _recognized_payload(record: PkulawRecognizedLaw) -> dict:
    return {
        "mentioned_title": record.mentioned_title,
        "canonical_title": record.canonical_title,
        "fulltext": record.fulltext,
        "url": record.url,
    }


def _recognized_from_payload(payload: dict) -> PkulawRecognizedLaw:
    return PkulawRecognizedLaw(**payload)


def _record_from_payload(payload: dict) -> PkulawLawRecord:
    return PkulawLawRecord(
        title=payload.get("title", ""),
        url=usable_mcp_url(payload.get("url")),
        category=payload.get("category", []),
        document_no=payload.get("document_no"),
        issue_department=payload.get("issue_department", []),
        issue_date=payload.get("issue_date"),
        implement_date=payload.get("implement_date"),
        timeliness=payload.get("timeliness", []),
        effectiveness=payload.get("effectiveness", []),
    )


def _article_payload(article: PkulawArticle) -> dict:
    return {
        **_record_payload(article),
        "article_no": article.article_no,
        "article_text": article.article_text,
    }


def _article_from_payload(payload: dict) -> PkulawArticle:
    base = _record_from_payload(payload)
    return PkulawArticle(
        title=base.title,
        url=base.url,
        category=base.category,
        document_no=base.document_no,
        issue_department=base.issue_department,
        issue_date=base.issue_date,
        implement_date=base.implement_date,
        timeliness=base.timeliness,
        effectiveness=base.effectiveness,
        article_no=payload.get("article_no", ""),
        article_text=payload.get("article_text", ""),
    )
