"""本地法规 SQLite 仓储及其确定性查询操作。"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..domain.legal_numbers import chinese_number_to_int
from ..domain.law_titles import cn_title_shape_variants

from .paths import PROJECT_ROOT


SCHEMA_VERSION = "1.2"

# 章节层级：编=0 分编=1 章=2 节=3
STRUCTURE_LEVELS = {"编": 0, "分编": 1, "章": 2, "节": 3}


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        _create_schema(conn)


def seed_common_laws(db_path: str | Path, catalog_path: str | Path | None = None) -> int:
    if catalog_path is None:
        catalog_path = PROJECT_ROOT / "laws" / "common_laws.json"
    records = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    init_db(db_path)
    with connect(db_path) as conn:
        count = 0
        for record in records:
            upsert_law(conn, record)
            count += 1
        return count


def find_law(conn: sqlite3.Connection, title: str) -> Optional[sqlite3.Row]:
    normalized = normalize_title(title)
    stripped = strip_version_annotation(normalized)
    for candidate in dict.fromkeys((
        *cn_title_shape_variants(normalized),
        *cn_title_shape_variants(stripped),
    )):
        if row := _find_law_by_normalized(conn, candidate):
            return row
    return None


def _find_law_by_normalized(
    conn: sqlite3.Connection, normalized: str
) -> Optional[sqlite3.Row]:
    row = conn.execute(
        "SELECT * FROM laws WHERE normalized_title = ? LIMIT 1",
        (normalized,),
    ).fetchone()
    if row:
        return row
    return conn.execute(
        """
        SELECT l.*
        FROM law_aliases a
        JOIN laws l ON l.id = a.law_id
        WHERE a.alias = ?
        LIMIT 1
        """,
        (normalized,),
    ).fetchone()


def find_current_article(
    conn: sqlite3.Connection,
    title: str,
    article_no: str,
    as_of: str | date | None = None,
) -> Optional[sqlite3.Row]:
    law = find_law(conn, title)
    if not law:
        return None
    as_of_text = _date_text(as_of) or date.today().isoformat()
    normalized_key = normalize_article_key(article_no)
    row = conn.execute(
        """
        SELECT
          a.id AS article_id,
          l.title,
          l.source_type,
          l.status AS law_status,
          a.article_no,
          a.article_key,
          a.text,
          a.version_key,
          a.version_label,
          a.version_status,
          a.source_name,
          a.source_url,
          a.source_fetched_at,
          a.timeliness,
          a.effectiveness,
          a.issued_at,
          a.effective_from,
          a.effective_to,
          a.effective_at
        FROM articles a
        JOIN laws l ON l.id = a.law_id
        WHERE a.law_id = ?
          AND a.article_key = ?
          AND (a.effective_from IS NULL OR a.effective_from <= ?)
          AND (a.effective_to IS NULL OR a.effective_to > ?)
        ORDER BY
          CASE WHEN a.effective_from IS NULL THEN 0 ELSE 1 END DESC,
          a.effective_from DESC,
          a.id DESC
        LIMIT 1
        """,
        (law["id"], normalized_key, as_of_text, as_of_text),
    ).fetchone()
    if row is not None:
        return row

    # 兼容 1.1 以前数据库中以中文条号保存的 article_key。数据库升级后
    # 新写入统一使用阿拉伯数字键，旧数据无需破坏性迁移也能正确命中。
    candidates = conn.execute(
        """
        SELECT
          a.id AS article_id,
          l.title, l.source_type, l.status AS law_status,
          a.article_no, a.article_key, a.text, a.version_key,
          a.version_label, a.version_status, a.source_name, a.source_url,
          a.source_fetched_at, a.timeliness, a.effectiveness, a.issued_at,
          a.effective_from, a.effective_to, a.effective_at
        FROM articles a
        JOIN laws l ON l.id = a.law_id
        WHERE a.law_id = ?
          AND (a.effective_from IS NULL OR a.effective_from <= ?)
          AND (a.effective_to IS NULL OR a.effective_to > ?)
        ORDER BY
          CASE WHEN a.effective_from IS NULL THEN 0 ELSE 1 END DESC,
          a.effective_from DESC,
          a.id DESC
        """,
        (law["id"], as_of_text, as_of_text),
    ).fetchall()
    return next(
        (
            candidate
            for candidate in candidates
            if normalize_article_key(candidate["article_no"]) == normalized_key
            or normalize_article_key(candidate["article_key"]) == normalized_key
        ),
        None,
    )


def list_current_articles(
    conn: sqlite3.Connection,
    title: str,
    as_of: str | date | None = None,
) -> list[sqlite3.Row]:
    """返回指定法规每个条款的最新有效版本。"""
    law = find_law(conn, title)
    if not law:
        return []
    as_of_text = _date_text(as_of) or date.today().isoformat()
    rows = conn.execute(
        """
        SELECT
          l.title,
          l.source_type,
          l.status AS law_status,
          a.article_no,
          a.article_key,
          a.text,
          a.version_key,
          a.version_label,
          a.version_status,
          a.source_name,
          a.source_url,
          a.source_fetched_at,
          a.timeliness,
          a.effectiveness,
          a.issued_at,
          a.effective_from,
          a.effective_to,
          a.effective_at
        FROM articles a
        JOIN laws l ON l.id = a.law_id
        WHERE a.law_id = ?
          AND (a.effective_from IS NULL OR a.effective_from <= ?)
          AND (a.effective_to IS NULL OR a.effective_to > ?)
        ORDER BY
          a.article_key,
          CASE WHEN a.effective_from IS NULL THEN 0 ELSE 1 END DESC,
          a.effective_from DESC,
          a.id DESC
        """,
        (law["id"], as_of_text, as_of_text),
    ).fetchall()
    latest_by_article: dict[str, sqlite3.Row] = {}
    for row in rows:
        canonical_key = normalize_article_key(row["article_no"] or row["article_key"])
        latest_by_article.setdefault(canonical_key, row)
    return list(latest_by_article.values())


def list_article_versions(
    conn: sqlite3.Connection,
    title: str,
    article_no: str,
) -> list[sqlite3.Row]:
    """按生效时间倒序返回指定条文的全部已存版本。"""
    law = find_law(conn, title)
    if not law:
        return []
    normalized_key = normalize_article_key(article_no)
    rows = conn.execute(
        """
        SELECT
          a.id AS article_id, l.title, l.source_type, l.status AS law_status,
          a.article_no, a.article_key, a.text, a.version_key,
          a.version_label, a.version_status, a.source_name, a.source_url,
          a.source_fetched_at, a.timeliness, a.effectiveness, a.issued_at,
          a.effective_from, a.effective_to, a.effective_at
        FROM articles a
        JOIN laws l ON l.id = a.law_id
        WHERE a.law_id = ?
        ORDER BY a.effective_from DESC, a.id DESC
        """,
        (law["id"],),
    ).fetchall()
    return [
        row for row in rows
        if normalize_article_key(row["article_no"] or row["article_key"]) == normalized_key
    ]


def list_all_articles(
    conn: sqlite3.Connection,
    title: str,
) -> list[sqlite3.Row]:
    """返回指定法规的全部条文、全部已存版本（纠错反推的本地全版本池）。"""
    law = find_law(conn, title)
    if not law:
        return []
    return conn.execute(
        """
        SELECT
          a.id AS article_id, l.title, l.source_type, l.status AS law_status,
          a.article_no, a.article_key, a.text, a.version_key,
          a.version_label, a.version_status, a.source_name, a.source_url,
          a.source_fetched_at, a.timeliness, a.effectiveness, a.issued_at,
          a.effective_from, a.effective_to, a.effective_at
        FROM articles a
        JOIN laws l ON l.id = a.law_id
        WHERE a.law_id = ?
        ORDER BY a.effective_from DESC, a.id DESC
        """,
        (law["id"],),
    ).fetchall()


def list_historical_article_versions(
    conn: sqlite3.Connection,
    title: str,
    article_no: str,
    as_of: str | date | None = None,
) -> list[sqlite3.Row]:
    """返回在指定日期前已经终止效力的条文版本。"""
    as_of_text = _date_text(as_of) or date.today().isoformat()
    return [
        row
        for row in list_article_versions(conn, title, article_no)
        if row["effective_to"] is not None and row["effective_to"] <= as_of_text
    ]


def upsert_law(conn: sqlite3.Connection, record: dict[str, Any]) -> int:
    now = _now()
    title = record["title"].strip()
    normalized_title = normalize_title(title)
    status = record.get("status", "catalog_only")
    conn.execute(
        """
        INSERT INTO laws (
          title, normalized_title, source_type, authority, category,
          priority, status, source_url, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(normalized_title) DO UPDATE SET
          title = excluded.title,
          source_type = excluded.source_type,
          authority = COALESCE(excluded.authority, laws.authority),
          category = COALESCE(excluded.category, laws.category),
          priority = COALESCE(excluded.priority, laws.priority),
          status = CASE
            WHEN laws.status = 'has_articles' AND excluded.status = 'catalog_only'
            THEN laws.status
            ELSE excluded.status
          END,
          source_url = COALESCE(excluded.source_url, laws.source_url),
          updated_at = excluded.updated_at
        """,
        (
            title,
            normalized_title,
            record.get("source_type", "other_normative_document"),
            record.get("authority"),
            record.get("category"),
            record.get("priority"),
            status,
            record.get("source_url"),
            now,
        ),
    )
    row = conn.execute(
        "SELECT id FROM laws WHERE normalized_title = ?",
        (normalized_title,),
    ).fetchone()
    law_id = int(row["id"])
    aliases = set(record.get("aliases", [])) | set(generate_aliases(title))
    for alias in aliases:
        upsert_alias(conn, law_id, alias, canonical_title=title)
    return law_id


def upsert_alias(
    conn: sqlite3.Connection,
    law_id: int,
    alias: str,
    canonical_title: str | None = None,
) -> None:
    normalized_alias = normalize_title(alias)
    if not normalized_alias:
        return
    if canonical_title and normalized_alias == normalize_title(canonical_title):
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO law_aliases (alias, law_id)
        VALUES (?, ?)
        """,
        (normalized_alias, law_id),
    )


def upsert_article(conn: sqlite3.Connection, law_id: int, record: dict[str, Any]) -> int:
    article_no = record["article_no"].strip()
    article_key = normalize_article_key(article_no)
    version_label = record.get("version_label")
    version_status = record.get("version_status")
    effective_from = record.get("effective_from")
    version_key = normalize_version_key(record.get("version_key") or effective_from or "current")
    text = record.get("text", "").strip()
    now = _now()
    conn.execute(
        """
        INSERT INTO articles (
          law_id, article_no, article_key, version_key, version_label,
          version_status, text, source_name, source_url, source_fetched_at,
          timeliness, effectiveness, issued_at, effective_from, effective_to,
          effective_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(law_id, article_key, version_key) DO UPDATE SET
          article_no = excluded.article_no,
          version_label = COALESCE(excluded.version_label, articles.version_label),
          version_status = COALESCE(excluded.version_status, articles.version_status),
          text = excluded.text,
          source_name = COALESCE(excluded.source_name, articles.source_name),
          source_url = COALESCE(excluded.source_url, articles.source_url),
          source_fetched_at = COALESCE(excluded.source_fetched_at, articles.source_fetched_at),
          timeliness = COALESCE(excluded.timeliness, articles.timeliness),
          effectiveness = COALESCE(excluded.effectiveness, articles.effectiveness),
          issued_at = COALESCE(excluded.issued_at, articles.issued_at),
          effective_from = COALESCE(excluded.effective_from, articles.effective_from),
          effective_to = excluded.effective_to,
          effective_at = COALESCE(excluded.effective_at, articles.effective_at),
          updated_at = excluded.updated_at
        """,
        (
            law_id,
            article_no,
            article_key,
            version_key,
            version_label,
            version_status,
            text,
            record.get("source_name"),
            record.get("source_url"),
            record.get("source_fetched_at"),
            version_label,
            record.get("effectiveness"),
            record.get("issued_at"),
            effective_from,
            record.get("effective_to"),
            effective_from,
            now,
        ),
    )
    conn.execute("UPDATE laws SET status = 'has_articles', updated_at = ? WHERE id = ?", (now, law_id))
    row = conn.execute(
        "SELECT id FROM articles WHERE law_id = ? AND article_key = ? AND version_key = ?",
        (law_id, article_key, version_key),
    ).fetchone()
    return int(row["id"])


# ============================================================
# 章节结构（schema 1.2）
# ============================================================

def delete_structures_for_law(
    conn: sqlite3.Connection, law_id: int, version_key: str
) -> None:
    """重建前清空某法某版本的全部章节节点（级联删除条文归属）。"""
    conn.execute(
        "DELETE FROM law_structures WHERE law_id = ? AND version_key = ?",
        (law_id, version_key),
    )


def upsert_structure_node(
    conn: sqlite3.Connection,
    law_id: int,
    record: dict[str, Any],
) -> int:
    """插入一个章节节点并计算物化路径；父节点必须先入库。"""
    node_type = record["node_type"]
    level = STRUCTURE_LEVELS[node_type]
    parent_id = record.get("parent_id")
    parent_path_ids, parent_path_label = "/", ""
    if parent_id is not None:
        parent = conn.execute(
            "SELECT path_ids, path_label FROM law_structures WHERE id = ?",
            (parent_id,),
        ).fetchone()
        if parent is None:
            raise ValueError(f"structure parent {parent_id} not found")
        parent_path_ids = parent["path_ids"]
        parent_path_label = parent["path_label"]
    label = " ".join(
        part for part in (record.get("number_text"), record.get("title")) if part
    ) or record["heading_text"]
    path_label = f"{parent_path_label} / {label}" if parent_path_label else label
    cursor = conn.execute(
        """
        INSERT INTO law_structures (
          law_id, version_key, parent_id, node_type, level, number,
          number_text, title, heading_text, seq, path_ids, path_label,
          updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)
        """,
        (
            law_id,
            record.get("version_key", "current"),
            parent_id,
            node_type,
            level,
            record.get("number"),
            record.get("number_text"),
            record.get("title"),
            record["heading_text"],
            record["seq"],
            path_label,
            _now(),
        ),
    )
    node_id = int(cursor.lastrowid)
    conn.execute(
        "UPDATE law_structures SET path_ids = ? WHERE id = ?",
        (f"{parent_path_ids}{node_id}/", node_id),
    )
    return node_id


def upsert_article_membership(
    conn: sqlite3.Connection,
    article_id: int,
    structure_id: int,
    law_id: int,
    version_key: str,
) -> None:
    conn.execute(
        """
        INSERT INTO article_structure_memberships (
          article_id, structure_id, law_id, version_key, updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(article_id) DO UPDATE SET
          structure_id = excluded.structure_id,
          law_id = excluded.law_id,
          version_key = excluded.version_key,
          updated_at = excluded.updated_at
        """,
        (article_id, structure_id, law_id, version_key, _now()),
    )


def find_structure_candidates(
    conn: sqlite3.Connection,
    law_id: int,
    node_type: str,
    number: int | None,
    version_key: str | None = None,
    parent_id: int | None = None,
) -> list[sqlite3.Row]:
    """按类型与序号查节点；章号只在父节点内唯一，裸查天然返回多候选。"""
    sql = ["SELECT * FROM law_structures WHERE law_id = ? AND node_type = ?"]
    params: list[Any] = [law_id, node_type]
    if number is None:
        sql.append("AND number IS NULL")
    else:
        sql.append("AND number = ?")
        params.append(number)
    if version_key is not None:
        sql.append("AND version_key = ?")
        params.append(version_key)
    if parent_id is not None:
        sql.append("AND parent_id = ?")
        params.append(parent_id)
    sql.append("ORDER BY seq")
    return conn.execute(" ".join(sql), params).fetchall()


def resolve_structure_path(
    conn: sqlite3.Connection,
    law_id: int,
    tokens: list[tuple[str, int | None]],
    version_key: str | None = None,
) -> list[sqlite3.Row]:
    """链式定位章节，如 [('编',3),('章',4)]；多候选原样返回，不猜测。"""
    current: list[sqlite3.Row] | None = None
    for node_type, number in tokens:
        if current is None:
            current = find_structure_candidates(
                conn, law_id, node_type, number, version_key
            )
        else:
            narrowed: dict[int, sqlite3.Row] = {}
            for ancestor in current:
                rows = conn.execute(
                    """
                    SELECT * FROM law_structures
                    WHERE law_id = ? AND node_type = ?
                      AND (? IS NULL OR version_key = ?)
                      AND ((? IS NULL AND number IS NULL) OR number = ?)
                      AND path_ids LIKE ? || '%'
                    ORDER BY seq
                    """,
                    (
                        law_id,
                        node_type,
                        version_key,
                        version_key,
                        number,
                        number,
                        ancestor["path_ids"],
                    ),
                ).fetchall()
                for row in rows:
                    narrowed.setdefault(int(row["id"]), row)
            current = sorted(narrowed.values(), key=lambda row: row["seq"])
        if not current:
            return []
    return current or []


def list_articles_in_structure(
    conn: sqlite3.Connection, structure_id: int
) -> list[sqlite3.Row]:
    """列出节点（含全部子孙）下的成员条文，按条号排序。"""
    node = conn.execute(
        "SELECT path_ids FROM law_structures WHERE id = ?", (structure_id,)
    ).fetchone()
    if node is None:
        return []
    rows = conn.execute(
        """
        SELECT a.*
        FROM articles a
        JOIN article_structure_memberships m ON m.article_id = a.id
        JOIN law_structures s ON s.id = m.structure_id
        WHERE s.path_ids LIKE ? || '%'
        """,
        (node["path_ids"],),
    ).fetchall()

    def order(row: sqlite3.Row) -> tuple[int, int]:
        # article_key 可能是 1.1 以前的中文遗留键，归一化后按数值排序
        key = normalize_article_key(row["article_no"] or row["article_key"])
        base, _, suffix = key.partition("-")
        try:
            return (int(base), int(suffix or 0))
        except ValueError:
            return (10**9, 0)

    return sorted(rows, key=order)


def get_structure_path_for_article(
    conn: sqlite3.Connection, article_id: int
) -> Optional[str]:
    """返回条文所属章节的完整路径标签，如'第三编 合同 / 第一章 一般规定'。"""
    row = conn.execute(
        """
        SELECT s.path_label
        FROM article_structure_memberships m
        JOIN law_structures s ON s.id = m.structure_id
        WHERE m.article_id = ?
        """,
        (article_id,),
    ).fetchone()
    return row["path_label"] if row else None


_TITLE_TRANSLATION = str.maketrans(
    {
        "《": "",
        "》": "",
        "〈": "",
        "〉": "",
        "＜": "",
        "＞": "",
        "<": "",
        ">": "",
        "﹤": "",
        "﹥": "",
        "(": "（",
        ")": "）",
    }
)


def normalize_title(title: str) -> str:
    """删除法名中的空白与书名号变体，并将半角圆括号统一为全角。"""
    return "".join(title.split()).translate(_TITLE_TRANSLATION)


_VERSION_ANNOTATION = re.compile(
    r"[（(](?:\d{4}年?)?(?:修正|修订|修改)(?:\d{4}年?)?[）)]$"
)


def strip_version_annotation(title: str) -> str:
    """剥离标题末尾的版本注记，如"（2025修正）""（2018年修订）"。"""
    return _VERSION_ANNOTATION.sub("", title)


def list_law_titles(conn: sqlite3.Connection) -> list[str]:
    """返回库内全部法规标题及别名（用于法名模糊纠错）。"""
    titles = [row[0] for row in conn.execute("SELECT title FROM laws")]
    titles.extend(row[0] for row in conn.execute("SELECT alias FROM law_aliases"))
    return titles


_FOREIGN_ARTICLE_PREFIX = re.compile(
    r"^(?:article|art\.?|§|section|sec\.?)\s*(\d+(?:[.\-]\d+)?)",
    re.IGNORECASE,
)


def normalize_article_key(article_no: str) -> str:
    """把中文/外文条号归一为同一键。

    第一百二十七条、第127条、Article 127、§ 4、Section 5 均归一为阿拉伯
    数字串，使跨法域条号可比较。中文「之」后缀保留为 N-M 形式。
    """
    compact = "".join(str(article_no).split())
    # 外文条号（Article / Art. / § / Section / Sec. + 数字）→ 裸数字
    foreign = _FOREIGN_ARTICLE_PREFIX.match(compact)
    if foreign:
        return foreign.group(1).replace("-", ".")
    compact = compact.removeprefix("第").replace("条", "")
    base, separator, suffix = compact.partition("之")
    base_number = chinese_number_to_int(base)
    if base_number is None:
        return compact
    if not separator:
        return str(base_number)
    suffix_number = chinese_number_to_int(suffix)
    return f"{base_number}-{suffix_number}" if suffix_number is not None else compact


def normalize_version_key(version_key: str) -> str:
    return "".join(version_key.split()) or "current"


def generate_aliases(title: str) -> Iterable[str]:
    if title.startswith("中华人民共和国") and len(title) > len("中华人民共和国") + 1:
        yield title.replace("中华人民共和国", "", 1)


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS schema_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS laws (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          title TEXT NOT NULL,
          normalized_title TEXT NOT NULL UNIQUE,
          source_type TEXT NOT NULL,
          authority TEXT,
          category TEXT,
          priority INTEGER,
          status TEXT NOT NULL DEFAULT 'catalog_only',
          source_url TEXT,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS law_aliases (
          alias TEXT PRIMARY KEY,
          law_id INTEGER NOT NULL REFERENCES laws(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS articles (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          law_id INTEGER NOT NULL REFERENCES laws(id) ON DELETE CASCADE,
          article_no TEXT NOT NULL,
          article_key TEXT NOT NULL,
          version_key TEXT NOT NULL DEFAULT 'current',
          version_label TEXT,
          version_status TEXT,
          text TEXT NOT NULL,
          source_name TEXT,
          source_url TEXT,
          source_fetched_at TEXT,
          timeliness TEXT,
          effectiveness TEXT,
          issued_at TEXT,
          effective_from TEXT,
          effective_to TEXT,
          effective_at TEXT,
          updated_at TEXT NOT NULL,
          UNIQUE(law_id, article_key, version_key)
        );

        CREATE TABLE IF NOT EXISTS law_structures (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          law_id        INTEGER NOT NULL REFERENCES laws(id) ON DELETE CASCADE,
          version_key   TEXT NOT NULL DEFAULT 'current',
          parent_id     INTEGER REFERENCES law_structures(id) ON DELETE CASCADE,
          node_type     TEXT NOT NULL,
          level         INTEGER NOT NULL,
          number        INTEGER,
          number_text   TEXT,
          title         TEXT,
          heading_text  TEXT NOT NULL,
          seq           INTEGER NOT NULL,
          path_ids      TEXT NOT NULL,
          path_label    TEXT NOT NULL,
          start_article_key TEXT,
          end_article_key   TEXT,
          article_count INTEGER NOT NULL DEFAULT 0,
          updated_at    TEXT NOT NULL,
          UNIQUE(law_id, version_key, parent_id, node_type, number)
        );

        CREATE TABLE IF NOT EXISTS article_structure_memberships (
          article_id   INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
          structure_id INTEGER NOT NULL REFERENCES law_structures(id) ON DELETE CASCADE,
          law_id       INTEGER NOT NULL REFERENCES laws(id) ON DELETE CASCADE,
          version_key  TEXT NOT NULL DEFAULT 'current',
          updated_at   TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_laws_priority ON laws(priority);
        CREATE INDEX IF NOT EXISTS idx_articles_law_key ON articles(law_id, article_key, effective_from);
        CREATE INDEX IF NOT EXISTS idx_structures_lookup ON law_structures(law_id, version_key, node_type, number);
        CREATE INDEX IF NOT EXISTS idx_structures_parent ON law_structures(parent_id);
        CREATE INDEX IF NOT EXISTS idx_structures_path ON law_structures(law_id, version_key, path_ids);
        CREATE INDEX IF NOT EXISTS idx_membership_structure ON article_structure_memberships(structure_id);
        CREATE INDEX IF NOT EXISTS idx_membership_law ON article_structure_memberships(law_id, version_key);

        INSERT OR REPLACE INTO schema_meta (key, value)
        VALUES ('schema_version', '{SCHEMA_VERSION}');
        """
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _date_text(value: str | date | None) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]
