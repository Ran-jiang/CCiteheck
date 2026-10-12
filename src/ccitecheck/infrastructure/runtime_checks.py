"""检查法规库、模型及外部数据源配置。"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import load_project_env
from .database import connect


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    message: str


def check_runtime(law_db: str | Path) -> list[CheckResult]:
    load_project_env()
    db_path = Path(law_db)
    results = [
        _check_law_database(db_path),
        _check_qwen_key(),
        _check_pkulaw_token(),
        _check_eurlex_gateway(),
        _check_ansvar_gateway(),
    ]
    return results


def _check_law_database(db_path: Path) -> CheckResult:
    if not db_path.exists():
        return CheckResult("law_db", False, f"not found: {db_path}")

    try:
        with connect(db_path) as conn:
            law_count = _count(conn, "laws")
            article_count = _count(conn, "articles")
            fulltext_count = conn.execute(
                "SELECT COUNT(*) FROM laws WHERE status = 'has_articles'"
            ).fetchone()[0]
            schema_row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
    except sqlite3.Error as exc:
        return CheckResult("law_db", False, f"SQLite error: {exc}")

    schema_version = schema_row["value"] if schema_row else "unknown"
    if law_count == 0 or article_count == 0:
        return CheckResult("law_db", False, "database is empty")

    return CheckResult(
        "law_db",
        True,
        f"{law_count} laws, {fulltext_count} with full text, {article_count} articles, schema {schema_version}",
    )


def _check_pkulaw_token() -> CheckResult:
    if os.getenv("PKULAW_ACCESS_TOKEN"):
        return CheckResult("pkulaw", True, "statute and case MCP sources configured")
    return CheckResult("pkulaw", True, "optional fallback not configured")


def _check_eurlex_gateway() -> CheckResult:
    if os.getenv("EURLEX_MCP_GATEWAY"):
        return CheckResult("eurlex", True, "EU statute source configured")
    return CheckResult(
        "eurlex", True, "optional EU source not configured (EU citations marked out of scope)"
    )


def _check_ansvar_gateway() -> CheckResult:
    if not os.getenv("ANSVAR_MCP_GATEWAY"):
        return CheckResult("ansvar", True, "optional multi-jurisdiction source not configured")
    has_auth = bool(
        os.getenv("ANSVAR_ACCESS_TOKEN")
        or (os.getenv("ANSVAR_CLIENT_ID") and os.getenv("ANSVAR_CLIENT_SECRET"))
    )
    message = (
        "multi-jurisdiction MCP source configured"
        if has_auth
        else "gateway configured; interactive OAuth required on first use"
    )
    return CheckResult("ansvar", True, message)


def _check_qwen_key() -> CheckResult:
    if os.getenv("DASHSCOPE_API_KEY") or os.getenv("LLM_API_KEY"):
        model = os.getenv("QWEN_MODEL") or os.getenv("LLM_MODEL", "qwen3.7-plus")
        return CheckResult("qwen", True, f"semantic checks configured with {model}")
    return CheckResult(
        "qwen",
        False,
        "语义核查默认开启但未配置 DASHSCOPE_API_KEY（见 .env.example）",
    )


def _count(conn: sqlite3.Connection, table_name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
