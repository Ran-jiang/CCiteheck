"""
CCiteheck 引用校验测试。

测试 validate_claim_document：
  19. 正常文档返回空列表；构造异常场景能检测
"""

from ccitecheck.domain.document import (
    Anchor, Block, BlockType, DocMeta, ParsedDocument,
)
from ccitecheck.recognition.arbitration import (
    arbitrate_claim_candidates,
    build_claim_document,
)
import pytest
from pydantic import ValidationError

from ccitecheck.domain.citation import (
    ArticleRef, CaseCitationEntities, CaseHoldingParaphraseEntities, CaseRef,
    CitationLocator, CitationOccurrence,
    Claim, ClaimCandidate, ClaimDocument, ClaimType, LegalSourceClaimEntities,
    LegalSource,
)
from ccitecheck.domain.citation_integrity import CitationIntegrityError
from ccitecheck.application.verify_claims import verify_claim_document
from ccitecheck.recognition.validators import validate_claim_document


def test_claim_type_rejects_wrong_entity_model():
    """claim_type 与 entities 必须是同一类核查任务。"""
    with pytest.raises(ValidationError, match="LegalSourceClaimEntities"):
        ClaimCandidate(
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            anchor_ids=["line00001"],
            entities=CaseCitationEntities(),
        )


def test_old_claim_schema_and_removed_fields_are_rejected():
    """旧快照不得被静默解释为新结构。"""
    with pytest.raises(ValidationError, match="0.6"):
        ClaimDocument.model_validate({
            "claim_meta": {
                "schema_version": "0.4",
                "extractor_version": "0.3",
            },
        })
    with pytest.raises(ValidationError, match="缺少版本字段"):
        ClaimDocument.model_validate({"claim_meta": {}, "claims": []})

    with pytest.raises(ValidationError, match="source_type"):
        LegalSource.model_validate({
            "title": "民法典",
            "source_type": "law",
        })


def test_recognition_resolver_is_preserved_for_debugging():
    source = LegalSource(title="民法典")
    assert source.model_dump()["recognition"]["resolver"] == "direct"


def test_articles_and_structures_are_mutually_exclusive():
    with pytest.raises(ValidationError, match="不能同时存在"):
        LegalSource(
            title="民法典",
            articles=[ArticleRef(article="第一条")],
            structures=[{"label": "第一章"}],
        )


def test_partial_citation_coverage_is_rejected_before_lookup():
    """识别出的条款不得因 citation 只生成一部分而静默丢失。"""
    text = "依据《民法典》第五百七十七条和第五百零九条，应当履行合同义务。"
    source = LegalSource(
        title="民法典",
        articles=[
            ArticleRef(article="第五百七十七条"),
            ArticleRef(article="第五百零九条"),
        ],
    )
    source.recognition.mention_span = (3, 6)
    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(
            legal_sources=[source],
            citations=[CitationOccurrence(
                law_title="民法典",
                locator=CitationLocator(article="第五百七十七条"),
                citation_span=(2, 14),
                span_status="located",
            )],
        ),
    )
    doc = _make_parsed_doc([text])
    claim_doc = build_claim_document(doc, [claim])

    violations = validate_claim_document(doc, claim_doc)
    assert any("第五百零九条" in item and "缺少" in item for item in violations)
    with pytest.raises(CitationIntegrityError, match="第五百零九条"):
        verify_claim_document(claim_doc, ":memory:", sources=[])


def test_case_holding_keeps_full_claim_as_the_comparison_text():
    text = "某案认为合同有效。"
    doc = _make_parsed_doc([text])
    candidate = ClaimCandidate(
        claim_type=ClaimType.CASE_HOLDING_PARAPHRASE,
        anchor_ids=["line00001"],
        entities=CaseHoldingParaphraseEntities(
            case_refs=[CaseRef(case_name="某案")],
        ),
    )

    claims = arbitrate_claim_candidates([candidate], doc)
    assert claims[0].claim_type == ClaimType.CASE_HOLDING_PARAPHRASE
    assert claims[0].text == text


def test_citation_locator_cannot_invent_paragraph_or_item():
    text = "依据《民法典》第一条第二款，应依法处理。"
    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(
            legal_sources=[LegalSource(
                title="民法典",
                articles=[ArticleRef(article="第一条")],
            )],
            citations=[CitationOccurrence(
                law_title="民法典",
                locator=CitationLocator(article="第一条", paragraph="第二款"),
                citation_span=(2, 12),
                span_status="located",
            )],
        ),
    )
    doc = _make_parsed_doc([text])
    violations = validate_claim_document(doc, build_claim_document(doc, [claim]))
    assert any("locator.paragraph" in item for item in violations)


# ============================================================
# 辅助函数
# ============================================================

def _make_parsed_doc(texts: list[str]) -> ParsedDocument:
    """用文本列表构建 ParsedDocument"""
    blocks = []
    anchors = []
    for i, text in enumerate(texts):
        block_id = f"b_{i+1:05d}"
        anchor_id = f"line{i+1:05d}"

        block = Block(
            block_id=block_id,
            type=BlockType.PARAGRAPH,
            text=text,
            style=None,
            section_path=[],
            body_order=i,
            block_order=i,
            para_index=i,
            table_index=None,
            row_index=None,
            cell_index=None,
            has_numbering=False,
            numbering_text=None,
            numbering_unresolved=False,
            is_list_item=False,
            list_group_id=None,
            is_article_start=False,
            heading_source=None,
            anchor_range=[anchor_id, anchor_id],
            sentence_anchors=[anchor_id],
        )
        blocks.append(block)

        anchor = Anchor(
            anchor=anchor_id,
            text=text,
            block_id=block_id,
            para_index=i,
            char_start=0,
            char_end=len(text),
        )
        anchors.append(anchor)

    return ParsedDocument(
        doc_meta=DocMeta(
            schema_version="0.1",
            source_file="test.docx",
            doc_hash="sha256:test",
        ),
        blocks=blocks,
        anchors=anchors,
        chunks=[],
    )


def _make_valid_claim(anchor_ids: list[str], text: str) -> Claim:
    """构建合法的 Claim"""
    return Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=anchor_ids,
        entities=LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title="民法典",

                    articles=[],
                )
            ]
        ),
    )


# ============================================================
# Test 19a: 正常文档 → 返回空列表
# ============================================================

def test_valid_claim_document_passes():
    """正常文档校验通过，返回空列表"""
    text = "依据《民法典》第五百七十七条，被告应当承担违约责任。"
    doc = _make_parsed_doc([text])

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title="民法典",

                    articles=[],
                )
            ]
        ),
    )

    from ccitecheck.recognition.arbitration import build_claim_document
    claim_doc = build_claim_document(doc, [claim])

    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) == 0, f"Expected 0 violations, got: {violations}"


# ============================================================
# Test 19b: anchor 不存在 → 检测到
# ============================================================

def test_detect_nonexistent_anchor():
    """anchor 不存在 → 校验发现"""
    text = "测试文本。"
    doc = _make_parsed_doc([text])

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line99999"],  # 不存在
        entities=LegalSourceClaimEntities(legal_sources=[]),
    )

    claim_doc = build_claim_document(doc, [claim])
    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) > 0
    assert any("不存在" in v for v in violations)


# ============================================================
# Test 19c: text 与拼接不符 → 检测到
# ============================================================

def test_detect_anchor_text_mismatch():
    """claim.text 与 anchor 拼接不一致 → 校验发现"""
    original_text = "原文内容。"
    doc = _make_parsed_doc([original_text])

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text="被篡改的文本",  # 与原文不一致
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(legal_sources=[]),
    )

    claim_doc = build_claim_document(doc, [claim])
    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) > 0
    assert any("text" in v.lower() for v in violations)


# ============================================================
# Test 19d: anchor 不连续 → 检测到
# ============================================================

def test_detect_non_contiguous_anchors():
    """anchor_ids 不连续 → 校验发现"""
    texts = ["第一句。", "第二句。", "第三句。"]
    doc = _make_parsed_doc(texts)

    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=texts[0] + texts[2],  # 跳过了第二句
        anchor_ids=["line00001", "line00003"],  # 不连续
        entities=LegalSourceClaimEntities(legal_sources=[]),
    )

    claim_doc = build_claim_document(doc, [claim])
    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) > 0
    assert any("连续" in v or "不连续" in v for v in violations)


# ============================================================
# Test 19e: claim_id 重复 → 检测到
# ============================================================

def test_detect_duplicate_claim_id():
    """claim_id 重复 → 校验发现"""
    text = "测试文本。"
    doc = _make_parsed_doc([text])

    def make_claim(cid):
        return Claim(
            claim_id=cid,
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text=text,
            anchor_ids=["line00001"],
            entities=LegalSourceClaimEntities(legal_sources=[]),
        )

    claim_doc = build_claim_document(doc, [
        make_claim("cl_00001"),
        make_claim("cl_00001"),  # 重复
    ])
    violations = validate_claim_document(doc, claim_doc)
    assert len(violations) > 0
    assert any("重复" in v for v in violations)
