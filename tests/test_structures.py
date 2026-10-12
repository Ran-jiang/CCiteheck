"""章节结构（schema 1.2）：识别、解析与判定的单元测试。"""

from pathlib import Path

from ccitecheck.application.verify_claims import verify_claim_document
from ccitecheck.domain.citation import (
    Claim,
    ClaimDocument,
    ClaimMeta,
    ClaimType,
    LegalSourceClaimEntities,
)
from ccitecheck.domain.evidence import LookupStatus
from ccitecheck.domain.statute_results import StatuteErrorCode
from ccitecheck.infrastructure import database as db
from ccitecheck.recognition.statutes import extract_legal_sources


def test_structure_refs_extracted_only_without_article():
    chained = extract_legal_sources("《中华人民共和国民法典》第三编第四章另有规定。")
    assert chained[0].structures[0].label == "第三编第四章"
    assert [(u.unit, u.number) for u in chained[0].structures[0].units] == [
        ("编", 3), ("章", 4),
    ]

    with_article = extract_legal_sources("《中华人民共和国民法典》第五百零九条规定。")
    assert with_article[0].articles and not with_article[0].structures

    plain = extract_legal_sources("依据《中华人民共和国民法典》的规定。")
    assert not plain[0].structures


def _structure_db(tmp_path: Path) -> Path:
    path = tmp_path / "laws.sqlite"
    db.init_db(path)
    with db.connect(path) as conn:
        law_id = db.upsert_law(conn, {"title": "中华人民共和国民法典", "source_type": "law"})
        bian1 = db.upsert_structure_node(conn, law_id, {
            "version_key": "v", "node_type": "编", "number": 1,
            "number_text": "第一编", "title": "总则", "heading_text": "第一编　总则", "seq": 1,
        })
        zhang1 = db.upsert_structure_node(conn, law_id, {
            "version_key": "v", "parent_id": bian1, "node_type": "章", "number": 4,
            "number_text": "第四章", "title": "非法人组织", "heading_text": "第四章　非法人组织", "seq": 2,
        })
        bian3 = db.upsert_structure_node(conn, law_id, {
            "version_key": "v", "node_type": "编", "number": 3,
            "number_text": "第三编", "title": "合同", "heading_text": "第三编　合同", "seq": 3,
        })
        zhang3 = db.upsert_structure_node(conn, law_id, {
            "version_key": "v", "parent_id": bian3, "node_type": "章", "number": 4,
            "number_text": "第四章", "title": "合同的履行", "heading_text": "第四章　合同的履行", "seq": 4,
        })
        for no, node in (("第一百零二条", zhang1), ("第五百零九条", zhang3)):
            article_id = db.upsert_article(conn, law_id, {
                "article_no": no, "text": "正文。", "version_key": "v",
            })
            db.upsert_article_membership(conn, article_id, node, law_id, "v")
        conn.commit()
    return path


def _claim_doc(text: str) -> ClaimDocument:
    return ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="d", source_doc_hash="sha256:d", source_file="t.docx"
        ),
        claims=[Claim(
            claim_id="cl_00001",
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text=text,
            anchor_ids=["line00001"],
            entities=LegalSourceClaimEntities(
                legal_sources=extract_legal_sources(text)
            ),
        )],
    )


def test_unique_structure_citation_passes_with_path(tmp_path: Path):
    doc = verify_claim_document(
        _claim_doc("《中华人民共和国民法典》第三编第四章另有规定。"),
        _structure_db(tmp_path),
        include_cases=False,
    )
    check = doc.statute_results[0]
    assert check.lookup_status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert check.outcome == "pass"
    assert not check.findings
    assert check.evidence.structure_path == "第三编 合同 / 第四章 合同的履行"
    assert [r.article_no for r in check.evidence.related_articles] == ["第五百零九条"]


def test_ambiguous_structure_citation_goes_to_manual(tmp_path: Path):
    doc = verify_claim_document(
        _claim_doc("依据《中华人民共和国民法典》第四章处理。"),
        _structure_db(tmp_path),
        include_cases=False,
    )
    check = doc.statute_results[0]
    assert check.findings[0].code == StatuteErrorCode.CITATION_HIERARCHY_ERROR
    assert check.outcome == "issue"
    assert check.evidence.structure_path.startswith("候选：")


def test_missing_structure_citation_reports_hierarchy_error(tmp_path: Path):
    doc = verify_claim_document(
        _claim_doc("《中华人民共和国民法典》第九编另有规定。"),
        _structure_db(tmp_path),
        include_cases=False,
    )
    check = doc.statute_results[0]
    assert check.lookup_status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
    assert check.findings[0].code == StatuteErrorCode.CITATION_HIERARCHY_ERROR
