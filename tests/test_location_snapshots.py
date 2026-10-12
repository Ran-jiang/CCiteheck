"""定位链黄金快照：编排重构前后，定位相关输出必须逐字节一致。

SNAPSHOT_UPDATE=1 重新生成基准；普通运行只做比对。
"""
import json
import os
from pathlib import Path

from ccitecheck.domain.citation import (
    Claim,
    ClaimDocument,
    ClaimMeta,
    ClaimType,
    LegalSourceClaimEntities,
)
from ccitecheck.infrastructure.database import connect, init_db, upsert_article, upsert_law
from ccitecheck.orchestration.scheduler import (
    _collect_check_items,
    _run_lookups,
    resolve_location_for_item,
    verify_claim_document,
)
from ccitecheck.recognition.spans import locate_claim_article_spans
from ccitecheck.recognition.statutes import extract_legal_sources
from ccitecheck.retrieval.sources.local_laws import LocalSQLiteSource

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
UPDATE = os.getenv("SNAPSHOT_UPDATE") == "1"

A = "当事人应当按照约定全面履行自己的义务"
B = "处理个人信息应当遵循合法正当必要和诚信原则"


def _claim(text: str) -> Claim:
    claim = Claim(
        claim_id="cl_snapshot",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=extract_legal_sources(text)),
    )
    locate_claim_article_spans(claim)
    return claim


def _make_db(tmp_path: Path, articles, version: str) -> Path:
    db = tmp_path / "snapshot.sqlite"
    init_db(db)
    with connect(db) as conn:
        law_id = upsert_law(conn, {"title": "示例法", "source_url": "https://example.test/law"})
        for number, body in articles:
            upsert_article(conn, law_id, {
                "article_no": number,
                "text": body,
                "version_key": version,
            })
    return db


def _clean(value):
    if isinstance(value, dict):
        return {
            key: "<timestamp>" if key == "fetched_at" else _clean(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _dump(model):
    return _clean(model.model_dump(mode="json"))


def _snapshot(name: str, data) -> None:
    path = SNAPSHOT_DIR / f"{name}.json"
    text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
    if UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        return
    assert text + "\n" == path.read_text(encoding="utf-8"), (
        f"snapshot mismatch: {name}（SNAPSHOT_UPDATE=1 可重新生成，生成后必须人工核对 diff）"
    )


def _run_document(tmp_path: Path, text: str, articles, version: str = "2024-01-01"):
    db = _make_db(tmp_path, articles, version)
    document = ClaimDocument(claim_meta=ClaimMeta(), claims=[_claim(text)])
    result = verify_claim_document(
        document,
        db,
        sources=[LocalSQLiteSource(db)],
        include_cases=False,
    )
    return [_dump(item) for item in result.statute_results]


def test_snapshot_content_repair_resolution(tmp_path):
    results = _run_document(tmp_path, f"《示例法》第一条规定{B}。", [
        ("第一条", A),
        ("第二条", B),
    ])
    _snapshot("document_content_repair_resolution", results)


def test_snapshot_standalone_paragraph_typo(tmp_path):
    results = _run_document(tmp_path, (
        "《示例法》第100款规定，非法人组织可以确定一人或者数人代表该组织从事民事活动。"
    ), [
        ("第100条", A),
        ("第105条", "非法人组织可以确定一人或者数人代表该组织从事民事活动。"),
    ])
    _snapshot("document_standalone_paragraph_typo", results)


def test_snapshot_current_renumbering_local_rank(tmp_path):
    claim_text = (
        "《示例法》第42条规定，行政机关作出责令停产停业、吊销许可证或者执照、"
        "较大数额罚款等行政处罚决定之前，应当告知当事人有要求举行听证的权利。"
    )
    results = _run_document(tmp_path, claim_text, [
        ("第42条", "行政处罚应当由具有行政执法资格的执法人员实施。"),
        ("第44条", "行政机关在作出行政处罚决定之前，应当告知当事人拟作出的行政处罚内容。"),
        ("第63条", "行政机关拟作出较大数额罚款、吊销许可证件、责令停产停业等行政处罚决定，应当告知当事人有要求听证的权利。"),
        ("第70条", "行政机关应当依法公开行政处罚决定。"),
    ])
    _snapshot("document_current_renumbering_local_rank", results)


def test_snapshot_competing_articles_pending(tmp_path):
    results = _run_document(tmp_path, f"《示例法》第一条规定{B}。", [
        ("第一条", A),
        ("第二条", B),
        ("第三条", B),
    ])
    _snapshot("document_competing_articles_pending", results)


def test_snapshot_local_current_version_confirms(tmp_path):
    results = _run_document(tmp_path, f"《示例法》第一条规定{B}。", [
        ("第一条", A),
        ("第二条", B),
    ], version="current")
    _snapshot("document_local_current_resolution", results)


def test_snapshot_location_stage_raw(tmp_path):
    """直接钉住定位链产物：每条 item 的 StatuteLocationResolution 与修复标记。"""
    db = _make_db(tmp_path, [("第一条", A), ("第二条", B)], "2024-01-01")
    document = ClaimDocument(claim_meta=ClaimMeta(), claims=[_claim(f"《示例法》第一条规定{B}。")])
    source = LocalSQLiteSource(db)
    items = _collect_check_items(document, db)
    lookups = _run_lookups([source], items, db)
    locator_source = next(
        (candidate for candidate in [source] if callable(getattr(candidate, "locate_candidates", None))),
        None,
    )
    resolutions = {}
    for index, item in enumerate(items):
        result, attempts = lookups[item.lookup_key]
        resolution = resolve_location_for_item(
            item, result, attempts,
            locator_source=locator_source, local=source, retry_only=False,
        )
        if resolution is not None:
            resolutions[index] = resolution
    data = {
        "resolutions": {str(key): _dump(value) for key, value in resolutions.items()},
        "items": [
            {
                "location_resolution": (
                    _dump(item.location_resolution) if item.location_resolution is not None else None
                ),
                "correction_evidence": (
                    _dump(item.correction_evidence) if item.correction_evidence is not None else None
                ),
                "repair_verified": item.repair_verified,
            }
            for item in items
        ],
    }
    _snapshot("location_stage_raw", data)
