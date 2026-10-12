from pathlib import Path

import pytest
from pydantic import ValidationError

from ccitecheck.domain.citation import (
    ArticleRef,
    Claim,
    ClaimDocument,
    ClaimMeta,
    ClaimType,
    LegalSource,
    LegalSourceClaimEntities,
)
from ccitecheck.domain.claims import RawClaim, RawLegalMention, from_legacy_claim
from ccitecheck.domain.evidence import (
    ArticleEvidence,
    ArticleExcerpt,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from ccitecheck.domain.queries import (
    RepairDiagnosis,
    RepairPlan,
    RerankBatch,
    RerankDecision,
    RetrievalQueryPlan,
    IdentityCandidate,
)
from ccitecheck.domain.statute_results import StatuteErrorCode
from ccitecheck.infrastructure.database import connect, init_db, upsert_law
from ccitecheck.orchestration.policies.retrieval import lookup_with_chain
from ccitecheck.query_construction.planners import primary_plan
from ccitecheck.query_construction.versioning import build_version_hypothesis
from ccitecheck.application.verify_claims import verify_claim_document
from ccitecheck.orchestration.scheduler import _explicit_version_hint
from ccitecheck.recognition.statutes import extract_legal_sources
from ccitecheck.retrieval.ranking import retrieve_relevant_articles
from ccitecheck.retrieval.sources import LookupRequest, LookupResult


@pytest.mark.parametrize(
    ("text", "raw_time"),
    [
        ("根据现行《刑法》的规定处理。", "现行"),
        ("根据2006年《刑法》的规定处理。", "2006年"),
        ("根据2006年1月《刑法》的规定处理。", "2006年1月"),
        ("根据2020年8月28日《刑法》的规定处理。", "2020年8月28日"),
    ],
)
def test_recognition_preserves_raw_time(text, raw_time):
    source = extract_legal_sources(text)[0]
    assert source.raw_time == raw_time
    claim = Claim(
        claim_id="cl_time",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[source]),
    )
    assert from_legacy_claim(claim).legal_mentions[0].raw_time == raw_time


def test_revision_suffix_is_a_version_hint_but_fact_date_is_not():
    source = extract_legal_sources(
        "依据《中华人民共和国民事诉讼法》（2017年修正）第四十条处理。"
    )[0]

    assert source.raw_time == "2017年修正"
    assert _explicit_version_hint(source.raw_time) == "2017年修正"
    assert _explicit_version_hint("2022年7月1日") is None
    assert build_version_hypothesis("中华人民共和国民法典", "2022年7月1日") is None
    version = build_version_hypothesis("中华人民共和国民事诉讼法", "2017年修正")
    assert version and version.year == 2017 and version.kind == "amended"


def test_bibliographic_hard_negative_is_excluded_during_recognition():
    text = "参见张明楷主编：《刑法》，法律出版社2026年版，第33页。"
    assert extract_legal_sources(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "参见张三：《行政法》，载《法学研究》2025年第3期，第45页。",
        "参见张三：《行政法》，载《某学报》第12卷第3期。",
        "参见张三：《行政法》，DOI:10.1234/abcd.2025.001。",
        "参见张三：《行政法》，ISSN 1234-567X。",
    ],
)
def test_structured_bibliographic_markers_are_excluded_during_recognition(text: str):
    assert extract_legal_sources(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "《出版社管理规定》明确出版社应依法经营。",
        "《行政法》由本书主编在第三期课程中讲解。",
        "《行政法》内容见第33页。",
        "《行政法》文中出现 DOI 和 ISSN 字样。",
    ],
)
def test_isolated_bibliographic_words_do_not_trigger_hard_negative(text: str):
    source = extract_legal_sources(text)[0]
    assert source.title


def test_article_level_bm25_prefers_criminal_capacity_rules():
    rows = [
        {"article_key": "17", "article_no": "第十七条", "text": "已满十六周岁的人犯罪，应当负刑事责任。已满十四周岁不满十六周岁的人实施特定犯罪应当负刑事责任。"},
        {"article_key": "18", "article_no": "第十八条", "text": "精神病人在不能辨认或者不能控制自己行为时不负刑事责任；醉酒的人犯罪，应当负刑事责任。"},
        {"article_key": "133", "article_no": "第一百三十三条之一", "text": "在道路上醉酒驾驶机动车的，处拘役，并处罚金。"},
        {"article_key": "383", "article_no": "第三百八十三条", "text": "对犯贪污罪的，根据情节轻重处罚。"},
    ]
    ranked = retrieve_relevant_articles("刑事责任年龄；刑事责任能力", rows, limit=4)
    assert {item.article_no for item in ranked[:2]} == {"第十七条", "第十八条"}


def test_related_reranker_hard_gate_keeps_only_strong_matches():
    source = RelatedCandidateSource()
    result, _ = lookup_with_chain(
        [source],
        LookupRequest(
            law_title="中华人民共和国刑法",
            query_text="刑事责任年龄；刑事责任能力",
        ),
        SelectCapacityReranker(),
    )
    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert [item.article_no for item in result.evidence.related_articles] == [
        "第十七条",
        "第十八条",
    ]


def test_related_reranker_may_reject_every_candidate():
    result, _ = lookup_with_chain(
        [RelatedCandidateSource()],
        LookupRequest(
            law_title="中华人民共和国刑法",
            query_text="不存在的法律问题",
        ),
        RejectAllReranker(),
    )
    assert result.status == LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
    assert result.evidence.related_articles == []
    assert result.trace.metadata["all_candidates_rejected"] is True


def test_related_reranker_failure_hides_transport_detail():
    result, _ = lookup_with_chain(
        [RelatedCandidateSource()],
        LookupRequest(
            law_title="中华人民共和国刑法",
            query_text="刑事责任年龄；刑事责任能力",
        ),
        FailedReranker(),
    )
    assert result.status == LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
    assert result.trace.message == "模型服务暂时不可用"
    assert result.trace.metadata["related_failure"] == "model_unavailable"
    user_payload = str({
        "trace": result.trace.model_dump(mode="json"),
        "evidence": (
            result.evidence.model_dump(mode="json") if result.evidence else None
        ),
    })
    assert "transport" not in user_payload.lower()


def test_real_planner_candidate_without_content_and_version_proof_stays_unverified(tmp_path: Path):
    correct = "最高人民法院、最高人民检察院关于办理诈骗刑事案件具体应用法律若干问题的解释"
    typo = correct.replace("若干", "若于")
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        upsert_law(conn, {"title": correct, "source_type": "judicial_interpretation"})
    claim = Claim(
        claim_id="cl_typo",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=f"2011年《{typo}》第六条规定诈骗既遂与未遂的处罚规则。",
        anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[LegalSource(
            title=typo,
            raw_time="2011年",
            articles=[ArticleRef(article="第六条")],
        )]),
    )
    checker = NoApplicationAfterRepairChecker(correct)
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(source_doc_id="typo", source_doc_hash="sha256:typo"),
            claims=[claim],
        ),
        db_path,
        sources=[RepairableSource(correct)],
        semantic_checker=checker,
    ).statute_results[0]
    assert result.lookup_status == LookupStatus.LAW_NOT_FOUND
    assert result.evidence is None
    assert result.correction_evidence.law_title == correct
    assert not any(item.code == StatuteErrorCode.LAW_NAME_ERROR for item in result.findings)
    assert checker.application_calls == 0
    assert "若于”疑似为“若干" in result.message


@pytest.mark.parametrize(("reason", "expected"), [
    ("title_typo", "法律名称引用错误，应为《正确法》。"),
    ("article_error", "条号引用错误，应为《正确法》第二条。"),
    ("version_issue", "所引版本已更新，应为《正确法》第二条。"),
])
def test_verified_repair_suggestion_follows_error_type(reason, expected):
    from ccitecheck.orchestration.scheduler import _CheckItem, _verified_repair_finding

    item = _CheckItem(
        claim=Claim(
            claim_id="cl_fix", claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text="依据《错法》第一条。", anchor_ids=["line1"],
            entities=LegalSourceClaimEntities(legal_sources=[]),
        ),
        law_title="错法", display_title="错法",
        article=ArticleRef(article="第一条"), article_no="第一条",
        not_verifiable=None, repair_reason=reason, repair_verified=True,
        repaired_title="正确法", repaired_article_no="第二条",
    )

    assert _verified_repair_finding(item) is None  # 仅设置标志不等于有验证证据。


def test_issuer_prefix_completion_is_not_reported_as_law_name_error(tmp_path: Path):
    short = "关于审理未成年人刑事案件具体应用法律若干问题的解释"
    full = f"最高人民法院{short}"
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        upsert_law(conn, {"title": full, "source_type": "judicial_interpretation"})
    claim = Claim(
        claim_id="cl_issuer",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=f"2006年《{short}》第六条规定未成年人刑事责任规则。",
        anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[LegalSource(
            title=short,
            raw_time="2006年",
            articles=[ArticleRef(article="第六条")],
        )]),
    )
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(source_doc_id="issuer", source_doc_hash="sha256:issuer"),
            claims=[claim],
        ),
        db_path,
        sources=[RepairableSource(full)],
        semantic_checker=IssuerCompletionChecker(full),
    ).statute_results[0]

    assert result.lookup_status == LookupStatus.LAW_NOT_FOUND
    assert result.correction_evidence.law_title == full
    assert result.findings == []
    assert result.outcome == "review"


def test_planner_models_forbid_unexpected_fields():
    with pytest.raises(ValidationError):
        RetrievalQueryPlan.model_validate({
            "route": "statute_related",
            "target_name": "刑法",
            "article_no": None,
            "query_text": "刑事责任年龄",
            "candidate_articles": [],
            "answer": "第十七条",
        })


def test_primary_planner_normalizes_current_version_hint():
    mention = RawLegalMention(
        mention_id="m1",
        raw_title="刑法",
        raw_time="现行",
        recognition_form="explicit",
    )
    plan, error = primary_plan(
        RawClaim(
            claim_id="c1",
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            raw_text="根据现行《刑法》，有关刑事责任年龄和刑事责任能力的规定。",
            legal_mentions=[mention],
        ),
        mention,
        [IdentityCandidate(title="中华人民共和国刑法", basis="lexicon", priority=1)],
        None,
        CurrentHintPlanner(),
    )
    assert error is None
    assert plan.version_hint == "current"


class CurrentHintPlanner:
    def plan_related(self, **kwargs):
        return RetrievalQueryPlan(
            route="statute_related",
            target_name=kwargs["target_name"],
            query_text="刑事责任年龄；刑事责任能力",
            version_hint="现行",
        )


class RelatedCandidateSource:
    def lookup(self, request):
        trace = SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="fake recall",
            status=LookupStatus.RELEVANT_ARTICLES_FOUND,
        )
        rows = [
            ("第十七条", "已满十六周岁的人犯罪，应当负刑事责任。"),
            ("第十八条", "精神病人在不能辨认或者不能控制自己行为时不负刑事责任。"),
            ("第一百三十三条之一", "醉酒驾驶机动车的，处拘役，并处罚金。"),
            ("第三百八十三条", "对犯贪污罪的，根据情节轻重处罚。"),
        ]
        evidence = ArticleEvidence(
            law_title=request.law_title,
            related_articles=[
                ArticleExcerpt(article_no=no, article_text=text, relevance_score=1.0)
                for no, text in rows
            ],
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)


class DuplicateArticleSource:
    def __init__(self, law_title, source_name):
        self.law_title = law_title
        self.source_name = source_name

    def lookup(self, request):
        trace = SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name=self.source_name,
            status=LookupStatus.RELEVANT_ARTICLES_FOUND,
        )
        evidence = ArticleEvidence(
            law_title=self.law_title,
            related_articles=[ArticleExcerpt(
                article_no="第二条",
                article_text="经营者在生产经营活动中，应当遵循公平、诚信的原则。",
                relevance_score=1.0,
            )],
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)


class SelectCapacityReranker:
    def rerank_related(self, query_text, candidates):
        return RerankBatch(results=[
            RerankDecision(
                candidate_id=item.candidate_id,
                relevant=item.article_no in {"第十七条", "第十八条"},
                score=0.98 if item.article_no in {"第十七条", "第十八条"} else 0.02,
            )
            for item in candidates
        ])


class RejectAllReranker:
    def rerank_related(self, query_text, candidates):
        return RerankBatch(results=[
            RerankDecision(candidate_id=item.candidate_id, relevant=False, score=0.01)
            for item in candidates
        ])


class KeepAllReranker:
    def rerank_related(self, query_text, candidates):
        return RerankBatch(results=[
            RerankDecision(candidate_id=item.candidate_id, relevant=True, score=1.0)
            for item in candidates
        ])


def test_related_candidates_dedupe_same_article_across_versioned_source_titles():
    result, _ = lookup_with_chain(
        [
            DuplicateArticleSource("中华人民共和国反不正当竞争法", "local"),
            DuplicateArticleSource("中华人民共和国反不正当竞争法（2025修订）", "pkulaw"),
        ],
        LookupRequest(
            law_title="中华人民共和国反不正当竞争法",
            query_text="经营者不得实施不正当竞争行为",
        ),
        KeepAllReranker(),
    )
    assert [item.article_no for item in result.evidence.related_articles] == ["第二条"]
    assert result.trace.metadata["candidate_count"] == 1
    assert next(iter(result.trace.metadata["candidate_channels"].values())) == [
        "local", "pkulaw"
    ]


class FailedReranker:
    def rerank_related(self, query_text, candidates):
        raise RuntimeError("transport_error: upstream connection reset")


class RepairableSource:
    def __init__(self, correct):
        self.correct = correct

    def lookup(self, request):
        found = request.law_title == self.correct and request.article_no == "第六条"
        status = LookupStatus.ARTICLE_FOUND if found else LookupStatus.LAW_NOT_FOUND
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="fake authority",
            status=status,
            metadata={"candidate_titles": [self.correct]},
        )
        evidence = ArticleEvidence(
            law_title=self.correct,
            article_no="第六条",
            article_text="诈骗既有既遂，又有未遂，分别达到不同量刑幅度的，依照处罚较重的规定处罚。",
            data_source=trace,
        ) if found else None
        return LookupResult(status, evidence, trace)


class RepairChecker:
    def __init__(self, correct):
        self.correct = correct

    def plan_repair(self, **kwargs):
        return RepairPlan(
            diagnosis=RepairDiagnosis(
                reason="title_typo",
                confidence=0.99,
                message="原文法规名称中的“若于”疑似为“若干”",
            ),
            retry_request=RetrievalQueryPlan(
                route="statute_exact",
                target_name=self.correct,
                article_no="第六条",
                version_hint="2011",
            ),
        )


class NoApplicationAfterRepairChecker(RepairChecker):
    def __init__(self, correct):
        super().__init__(correct)
        self.application_calls = 0

    def compare_application(self, original_text, authorities):
        self.application_calls += 1
        raise AssertionError("确定性错误不应触发适用核查")

class IssuerCompletionChecker(RepairChecker):
    def plan_repair(self, **kwargs):
        return RepairPlan(
            diagnosis=RepairDiagnosis(
                reason="title_typo",
                confidence=0.99,
                message="已补全司法解释发布机关",
            ),
            retry_request=RetrievalQueryPlan(
                route="statute_exact",
                target_name=self.correct,
                article_no="第六条",
                version_hint="2006",
            ),
        )
