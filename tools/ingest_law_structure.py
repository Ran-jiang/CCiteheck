"""法规章节结构迁移工具：解析官方 DOCX → 入库 → 验证 → 清洗正文。

用法：
    PYTHONPATH=src python3 tools/ingest_law_structure.py \
        --db data/laws.sqlite --docx-dir "laws/docx 原文"

严格执行顺序：解析 → 写 law_structures → 写 memberships（含范围回填）
→ 四重验证 → 全部通过才用干净正文覆盖 articles.text（事务 + 备份）。
验证不过时结构表保留供排查，正文一律不动。

解析状态机改编自 git 历史 statutedb/law_parser.py（0b6d0f8），并补充：
目录区跳过（"目　录"标记 → 首个标题重复出现处恢复）、附则/总则无号
节点、司法解释"一、一般规定"编号章节。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from docx import Document  # noqa: E402

from ccitecheck.domain.legal_numbers import chinese_number_to_int  # noqa: E402
from ccitecheck.infrastructure import database as db  # noqa: E402

_CN_NUM = r"[零一二两三四五六七八九十百千万0-9]+"
_ARTICLE_START = re.compile(
    rf"^第({_CN_NUM})条(?:之({_CN_NUM}))?(?![零一二两三四五六七八九十百千万0-9])[　\s]*"
)
_SECTION = re.compile(rf"^(第({_CN_NUM})(编|分编|章|节))([　\s]*)(.*)$")
_PLAIN_SECTION = re.compile(r"^(附|总)[\s　]*则$")
_NUMBERED_SECTION = re.compile(r"^([一二三四五六七八九十]+)、([^。，；：、]{1,20})$")
_ITEM = re.compile(r"^[（(]([零一二两三四五六七八九十0-9]+)[）)]")
_TOC = re.compile(r"^目[\s　]*录$")

LEVELS = db.STRUCTURE_LEVELS


@dataclass
class NodeEvent:
    level: int
    node_type: str
    number: int | None
    number_text: str | None
    title: str | None
    heading_text: str


@dataclass
class ArticleEvent:
    label: str
    num: int
    suffix: int
    lines: list[str] = field(default_factory=list)

    @property
    def clean_text(self) -> str:
        return "\n".join(self.lines).strip()


@dataclass
class ParsedLaw:
    title: str
    events: list = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def articles(self) -> list[ArticleEvent]:
        return [e for e in self.events if isinstance(e, ArticleEvent)]

    @property
    def nodes(self) -> list[NodeEvent]:
        return [e for e in self.events if isinstance(e, NodeEvent)]


def _looks_like_heading(line: str, separator: str, rest: str) -> bool:
    """标题判定：标记后有空白分隔、无句读、长度受限。

    顿号是真实标题的常见字符（"设立、变更、转让和消灭"），必须放行；
    "第一章规定的…"这类正文引用靠"无分隔空白"和句读排除。
    """
    if len(line) > 40:
        return False
    if any(punct in rest for punct in "。，；："):
        return False
    if rest and not separator:
        return False
    return True


def _heading_event(line: str, top_level_type: str) -> NodeEvent | None:
    m = _SECTION.match(line)
    if m and _looks_like_heading(line, m.group(4), m.group(5)):
        number = chinese_number_to_int(m.group(2))
        if number is None:
            return None
        title = re.sub(r"[\s　]+", "", m.group(5)) or None
        return NodeEvent(
            level=LEVELS[m.group(3)],
            node_type=m.group(3),
            number=number,
            number_text=m.group(1),
            title=title,
            heading_text=line,
        )
    if _PLAIN_SECTION.match(line):
        title = re.sub(r"[\s　]+", "", line)
        return NodeEvent(
            level=LEVELS[top_level_type],
            node_type=top_level_type,
            number=None,
            number_text=None,
            title=title,
            heading_text=line,
        )
    nm = _NUMBERED_SECTION.match(line)
    if nm:
        number = chinese_number_to_int(nm.group(1))
        return NodeEvent(
            level=LEVELS["章"],
            node_type="章",
            number=number,
            number_text=f"{nm.group(1)}、",
            title=nm.group(2),
            heading_text=line,
        )
    return None


def parse_docx(path: Path) -> ParsedLaw:
    lines = [p.text.strip() for p in Document(str(path)).paragraphs]
    non_empty = [ln for ln in lines if ln]
    if not non_empty:
        raise ValueError(f"empty docx: {path}")
    # 标题以文件名为准（司法解释首行常跨段），去掉尾部 _日期 后缀
    title = db.normalize_title(path.stem.rsplit("_", 1)[0])
    parsed = ParsedLaw(title=title)

    # 先扫一遍确定顶层类型（有编则附则挂编级，否则挂章级）
    top_level_type = "编" if any(
        _SECTION.match(ln) and _SECTION.match(ln).group(3) == "编"
        for ln in non_empty
    ) else "章"

    toc_mode = False
    toc_first_heading: str | None = None
    current: ArticleEvent | None = None
    seen_article = False

    for line in non_empty[1:]:
        if _TOC.match(line):
            toc_mode = True
            toc_first_heading = None
            continue
        if toc_mode:
            # 目录区：跳过标题清单，直到首个目录标题在正文中重现
            if toc_first_heading is None:
                if _heading_event(line, top_level_type) is not None:
                    toc_first_heading = line
                continue
            if line == toc_first_heading:
                toc_mode = False  # 正文从这里开始，继续按正常标题处理
            else:
                continue

        m = _ARTICLE_START.match(line)
        if m:
            num = chinese_number_to_int(m.group(1))
            suffix = chinese_number_to_int(m.group(2)) if m.group(2) else 0
            if num is None or (m.group(2) and suffix is None):
                parsed.warnings.append(f"条号解析失败: {line[:30]}")
            else:
                label = f"第{m.group(1)}条" + (f"之{m.group(2)}" if m.group(2) else "")
                current = ArticleEvent(label=label, num=num, suffix=suffix or 0)
                rest = line[m.end():].strip()
                if rest:
                    current.lines.append(rest)
                parsed.events.append(current)
                seen_article = True
                continue

        node = _heading_event(line, top_level_type)
        if node is not None:
            parsed.events.append(node)
            current = None  # 新章节开始，后续散文不再并入上一条
            continue

        if current is not None:
            current.lines.append(line)
        elif not seen_article:
            pass  # 条文前的颁布信息等元数据，结构迁移不需要

    _check_continuity(parsed)
    return parsed


def _check_continuity(parsed: ParsedLaw) -> None:
    prev: tuple[int, int] | None = None
    for art in parsed.articles:
        cur = (art.num, art.suffix)
        if prev is not None and cur not in (
            (prev[0] + 1, 0),
            (prev[0], prev[1] + 1),
        ):
            kind = "非递增" if cur <= prev else "跳跃"
            parsed.warnings.append(f"条号{kind}: {prev} → {art.label}")
        prev = cur
    if not parsed.articles:
        parsed.warnings.append("未解析到任何条文")


# ============================================================
# 入库与验证
# ============================================================

@dataclass
class LawReport:
    title: str
    node_count: int = 0
    article_count: int = 0
    matched: int = 0
    docx_only: list[str] = field(default_factory=list)
    db_only: list[str] = field(default_factory=list)
    unassigned: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.docx_only and not self.db_only


def ingest_law(conn, parsed: ParsedLaw, report: LawReport) -> dict[int, str]:
    """写结构与归属；返回 {article_id: 干净正文}（清洗阶段使用）。"""
    law = db.find_law(conn, parsed.title)
    if law is None:
        report.errors.append(f"库中找不到法规: {parsed.title}")
        return {}
    law_id = int(law["id"])
    versions = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT version_key FROM articles WHERE law_id = ?", (law_id,)
        )
    ]
    if len(versions) != 1:
        report.errors.append(f"版本数异常({len(versions)}): {versions}")
        return {}
    version_key = versions[0]

    db.delete_structures_for_law(conn, law_id, version_key)

    stack: list[tuple[int, int]] = []  # (level, node_id)
    seq = 0
    node_members: dict[int, list[tuple[int, int, str]]] = {}
    clean_texts: dict[int, str] = {}
    # 库内 article_key 可能是 1.1 以前的中文数字遗留键，两侧统一归一化后匹配
    db_articles = {
        db.normalize_article_key(row["article_no"] or row["article_key"]): int(row["id"])
        for row in conn.execute(
            "SELECT id, article_no, article_key FROM articles WHERE law_id = ? AND version_key = ?",
            (law_id, version_key),
        )
    }
    matched_keys: set[str] = set()

    for event in parsed.events:
        if isinstance(event, NodeEvent):
            while stack and stack[-1][0] >= event.level:
                stack.pop()
            seq += 1
            node_id = db.upsert_structure_node(
                conn,
                law_id,
                {
                    "version_key": version_key,
                    "parent_id": stack[-1][1] if stack else None,
                    "node_type": event.node_type,
                    "number": event.number,
                    "number_text": event.number_text,
                    "title": event.title,
                    "heading_text": event.heading_text,
                    "seq": seq,
                },
            )
            node_members[node_id] = []
            stack.append((event.level, node_id))
            report.node_count += 1
            continue

        key = db.normalize_article_key(event.label)
        article_id = db_articles.get(key)
        if article_id is None:
            report.docx_only.append(event.label)
            continue
        matched_keys.add(key)
        clean_texts[article_id] = event.clean_text
        if stack:
            leaf = stack[-1][1]
            db.upsert_article_membership(conn, article_id, leaf, law_id, version_key)
            for _, node_id in stack:  # 范围与计数按祖先链累计
                node_members[node_id].append((event.num, event.suffix, key))
        else:
            report.unassigned += 1

    report.article_count = len(parsed.articles)
    report.matched = len(matched_keys)
    report.db_only = sorted(set(db_articles) - matched_keys, key=_key_order)
    report.warnings.extend(parsed.warnings)

    for node_id, members in node_members.items():
        if not members:
            report.errors.append(f"空章节节点 id={node_id}")
            continue
        conn.execute(
            """
            UPDATE law_structures
            SET start_article_key = ?, end_article_key = ?, article_count = ?
            WHERE id = ?
            """,
            (members[0][2], members[-1][2], len(members), node_id),
        )
        # 范围连续性：成员在全法条文序中必须构成连续区段
        orders = [(num, suffix) for num, suffix, _ in members]
        if orders != sorted(orders):
            report.errors.append(f"节点 id={node_id} 条文顺序错乱")
    return clean_texts


def _key_order(key: str):
    base, _, suffix = key.partition("-")
    try:
        return (int(base), int(suffix or 0))
    except ValueError:
        return (10**9, 0)


def validate_law(conn, parsed: ParsedLaw, report: LawReport) -> None:
    law = db.find_law(conn, parsed.title)
    if law is None:
        return
    law_id = int(law["id"])
    if report.node_count:
        missing = conn.execute(
            """
            SELECT COUNT(*) FROM articles a
            WHERE a.law_id = ?
              AND NOT EXISTS (
                SELECT 1 FROM article_structure_memberships m
                WHERE m.article_id = a.id
              )
            """,
            (law_id,),
        ).fetchone()[0]
        if missing:
            report.errors.append(f"{missing} 条条文没有章节归属")
        # 兄弟节点范围不重叠：按 seq 排序后 start 必须单调
        rows = conn.execute(
            """
            SELECT parent_id, start_article_key, end_article_key
            FROM law_structures WHERE law_id = ? ORDER BY parent_id, seq
            """,
            (law_id,),
        ).fetchall()
        by_parent: dict = {}
        for row in rows:
            by_parent.setdefault(row["parent_id"], []).append(row)
        for siblings in by_parent.values():
            prev_end = None
            for row in siblings:
                start = _key_order(row["start_article_key"] or "")
                if prev_end is not None and start <= prev_end:
                    report.errors.append(
                        f"兄弟节点范围重叠: {row['start_article_key']}"
                    )
                prev_end = _key_order(row["end_article_key"] or "")


def spot_checks(conn, reports: dict[str, LawReport]) -> list[str]:
    errors = []

    def count_nodes(law_title: str, node_type: str) -> int:
        law = db.find_law(conn, law_title)
        if law is None:
            return -1
        return conn.execute(
            "SELECT COUNT(*) FROM law_structures WHERE law_id = ? AND node_type = ?",
            (int(law["id"]), node_type),
        ).fetchone()[0]

    minfa_bian = count_nodes("中华人民共和国民法典", "编")
    if minfa_bian != 8:  # 7 编 + 无号附则（编级）
        errors.append(f"民法典编级节点数 {minfa_bian}，预期 8（7编+附则）")
    if count_nodes("中华人民共和国民事诉讼法", "节") <= 0:
        errors.append("民事诉讼法未解析出节级节点")
    if count_nodes("中华人民共和国公司法", "章") < 10:
        errors.append("公司法章数异常")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        errors.append(f"integrity_check: {integrity}")
    return errors


def clean_article_texts(conn, all_clean: dict[int, str]) -> int:
    changed = 0
    for article_id, text in all_clean.items():
        if not text:
            continue
        row = conn.execute(
            "SELECT text FROM articles WHERE id = ?", (article_id,)
        ).fetchone()
        if row and row["text"] != text:
            conn.execute(
                "UPDATE articles SET text = ?, updated_at = ? WHERE id = ?",
                (text, datetime.now(timezone.utc).isoformat(), article_id),
            )
            changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/laws.sqlite")
    parser.add_argument("--docx-dir", default="laws/docx 原文")
    parser.add_argument("--no-clean", action="store_true", help="只建结构，不清洗正文")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    docx_files = sorted(Path(args.docx_dir).glob("*.docx"))
    if not docx_files:
        print(f"错误: {args.docx_dir} 下没有 DOCX")
        return 1

    if not args.no_backup:
        backup = db_path.with_name(
            f"{db_path.name}.backup-{datetime.now():%Y%m%d%H%M%S}"
        )
        shutil.copy2(db_path, backup)
        print(f"已备份 → {backup}")

    db.init_db(db_path)  # 幂等迁移到 schema 1.2
    reports: dict[str, LawReport] = {}
    all_clean: dict[int, str] = {}

    with db.connect(db_path) as conn:
        for path in docx_files:
            parsed = parse_docx(path)
            report = LawReport(title=parsed.title)
            reports[parsed.title] = report
            clean = ingest_law(conn, parsed, report)
            validate_law(conn, parsed, report)
            all_clean.update(clean)
        conn.commit()

        errors = spot_checks(conn, reports)
        failed = [r for r in reports.values() if not r.ok]

        print(f"\n== 共 {len(reports)} 部法规 ==")
        for report in reports.values():
            flag = "OK " if report.ok else "FAIL"
            print(
                f"{flag} {report.title[:28]:30s} 节点 {report.node_count:3d} "
                f"条文 {report.matched}/{report.article_count}"
                + (f" 未归属 {report.unassigned}" if report.unassigned else "")
            )
            for err in report.errors:
                print(f"     ERROR: {err}")
            if report.docx_only:
                print(f"     DOCX 独有: {report.docx_only[:5]}")
            if report.db_only:
                print(f"     DB 独有: {report.db_only[:5]}")
            for warning in report.warnings[:3]:
                print(f"     warn: {warning}")
        for err in errors:
            print(f"抽查 ERROR: {err}")

        if failed or errors:
            print("\n验证未全部通过，正文保持原样（结构表保留供排查）。")
            return 1

        if args.no_clean:
            print("\n验证全部通过（--no-clean，跳过正文清洗）。")
            return 0

        changed = clean_article_texts(conn, all_clean)
        conn.commit()
        polluted = conn.execute(
            """
            SELECT COUNT(*) FROM articles
            WHERE text GLOB '*
第*[编章节]　*' OR text GLOB '*
第*[编章节]'
            """
        ).fetchone()[0]
        print(f"\n验证全部通过；已用干净正文覆盖 {changed} 条。残留疑似标题尾巴: {polluted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
