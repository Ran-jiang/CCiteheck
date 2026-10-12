from ccitecheck.domain.citation import (
    AliasDeclaration,
    ArticleRef,
    Claim,
    ClaimType,
    LegalSource,
    LegalSourceClaimEntities,
)
from ccitecheck.domain.claims import (
    InheritedFrom,
    RawClaim,
    RawClaimDocument,
    RawLegalMention,
    from_legacy_claim,
)
from ccitecheck.domain.queries import QueryExtractionFill, QueryInference
from ccitecheck.query_construction.name_resolution import LawLexicon, LawLexiconEntry
from ccitecheck.query_construction.service import (
    QueryResources,
    build_document_context,
    build_initial_hypothesis,
)
from ccitecheck.recognition.spans import locate_claim_article_spans


def test_legacy_projection_preserves_occurrence_role_and_span():
    claim = Claim(
        claim_id="c1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text="《公司法》第十条第一款规定甲。同条第二款规定乙。",
        anchor_ids=["a1"],
        entities=LegalSourceClaimEntities(legal_sources=[LegalSource(
            title="公司法",
            articles=[ArticleRef(
                article="第十条", paragraphs=["第一款", "第二款"]
            )],
        )]),
    )
    locate_claim_article_spans(claim)

    raw = from_legacy_claim(claim)

    assert [item.mention_id for item in raw.legal_mentions] == [
        citation.mention_id for citation in claim.entities.citations
    ]
    assert [item.role for item in raw.legal_mentions] == ["direct", "carry_forward"]
    assert [item.citation_span for item in raw.legal_mentions] == [
        citation.citation_span for citation in claim.entities.citations
    ]


def test_document_alias_is_visible_to_later_claim():
    first = RawClaim(
        claim_id="c1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text="《生成式人工智能服务管理暂行办法》（以下简称《暂行办法》）。",
        anchor_ids=["a1"],
        alias_declarations=[AliasDeclaration(
            full_name_raw="生成式人工智能服务管理暂行办法",
            alias_raw="暂行办法",
        )],
        legal_mentions=[RawLegalMention(
            mention_id="m1", raw_title="生成式人工智能服务管理暂行办法",
            recognition_form="explicit",
        )],
    )
    second = RawClaim(
        claim_id="c2",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text="《暂行办法》第二条规定……",
        anchor_ids=["a2"],
        legal_mentions=[RawLegalMention(
            mention_id="m2", raw_title="暂行办法", article_raw="第二条",
            recognition_form="explicit",
        )],
    )
    document = RawClaimDocument(source_claim_doc_id="d", claims=[first, second])
    context = build_document_context(document)
    resources = QueryResources(lexicon=LawLexicon([]))

    hypothesis = build_initial_hypothesis(
        second, resources, mention_id="m2", document_context=context
    )

    assert hypothesis.query_plan.target_name == "生成式人工智能服务管理暂行办法"
    assert any(item.basis == "document_alias" for item in hypothesis.identity_candidates)


class _CountingInference:
    def __init__(self):
        self.calls = 0

    def infer_query(self, **kwargs):
        self.calls += 1
        return QueryInference(
            canonical_title="中华人民共和国公司法", jurisdiction="CN"
        )


def test_inherited_mention_reuses_parent_hypothesis_without_second_inference():
    parent = RawClaim(
        claim_id="c1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text="《公司法》第十条规定……",
        anchor_ids=["a1"],
        legal_mentions=[RawLegalMention(
            mention_id="m1", raw_title="公司法", article_raw="第十条",
            recognition_form="explicit", mention_span=(0, 5),
        )],
    )
    child = RawClaim(
        claim_id="c2",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text="该法第十一条规定……",
        anchor_ids=["a2"],
        legal_mentions=[RawLegalMention(
            mention_id="m2", raw_title="公司法", article_raw="第十一条",
            recognition_form="inherited",
            inherited_from=InheritedFrom(anchor_id="a1"),
        )],
    )
    context = build_document_context(RawClaimDocument(
        source_claim_doc_id="d", claims=[parent, child]
    ))
    resources = QueryResources(lexicon=LawLexicon([]))
    planner = _CountingInference()

    parent_hypothesis = build_initial_hypothesis(
        parent, resources, mention_id="m1", planner=planner,
        document_context=context,
    )
    child_hypothesis = build_initial_hypothesis(
        child, resources, mention_id="m2", planner=planner,
        document_context=context,
    )

    assert planner.calls == 1
    assert child_hypothesis.identity_candidates == parent_hypothesis.identity_candidates
    assert child_hypothesis.jurisdiction.basis == ["inherited_hypothesis"]


class _HallucinatingPlanner:
    def extract_query_facts(self, **kwargs):
        return QueryExtractionFill(raw_title="模型脑补法")

    def infer_query(self, **kwargs):
        return QueryInference(canonical_title="模型编造法", jurisdiction="CN")


def test_model_fill_requires_source_substring_and_title_candidate_is_whitelisted():
    claim = RawClaim(
        claim_id="c1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text="依据星河数据治理法第十条处理。",
        anchor_ids=["a1"],
        legal_mentions=[RawLegalMention(
            mention_id="m1", raw_title_candidate="星河数据治理法",
            article_raw="第十条", recognition_form="bare",
        )],
    )

    hypothesis = build_initial_hypothesis(
        claim,
        QueryResources(lexicon=LawLexicon([])),
        mention_id="m1",
        planner=_HallucinatingPlanner(),
    )

    assert all(item.title != "模型脑补法" for item in hypothesis.identity_candidates)
    assert all(item.title != "模型编造法" for item in hypothesis.identity_candidates)
    assert hypothesis.query_plan.route == "statute_exact"


class _FailingPlanner:
    def infer_query(self, **kwargs):
        raise RuntimeError("offline")


def test_model_failure_keeps_deterministic_query_plan():
    claim = RawClaim(
        claim_id="c1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text="依据星河数据治理法第十条处理。",
        legal_mentions=[RawLegalMention(
            mention_id="m1", raw_title="星河数据治理法",
            article_raw="第十条", recognition_form="explicit",
        )],
    )

    hypothesis = build_initial_hypothesis(
        claim,
        QueryResources(lexicon=LawLexicon([])),
        mention_id="m1",
        planner=_FailingPlanner(),
    )

    assert hypothesis.query_plan.route == "statute_exact"
    assert hypothesis.query_plan.target_name == "中华人民共和国星河数据治理法"
    assert any(item.code == "inference_failure" for item in hypothesis.assumptions)


class _ForeignJurisdictionPlanner:
    def infer_query(self, **kwargs):
        return QueryInference(jurisdiction="DE")


def test_model_may_correct_only_default_cn_jurisdiction():
    resources = QueryResources(lexicon=LawLexicon([]))
    planner = _ForeignJurisdictionPlanner()
    weak = RawClaim(
        claim_id="weak",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text="《著作权法》第十条规定……",
        legal_mentions=[RawLegalMention(
            mention_id="m1", raw_title="著作权法", article_raw="第十条",
            recognition_form="explicit", mention_span=(0, 7),
        )],
    )
    explicit_cn = RawClaim(
        claim_id="strong",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text="我国《著作权法》第十条规定……",
        legal_mentions=[RawLegalMention(
            mention_id="m2", raw_title="著作权法", article_raw="第十条",
            recognition_form="explicit", mention_span=(2, 9),
        )],
    )

    corrected = build_initial_hypothesis(
        weak, resources, mention_id="m1", planner=planner
    )
    preserved = build_initial_hypothesis(
        explicit_cn, resources, mention_id="m2", planner=planner
    )

    assert corrected.jurisdiction.code == "DE"
    assert corrected.jurisdiction.basis == ["llm_inference_from_weak_signal"]
    assert corrected.query_plan.target_name == "著作权法"
    assert preserved.jurisdiction.code == "CN"
    assert preserved.jurisdiction.basis == ["adjacent_prefix"]


class _SharedSourcePlanner:
    def __init__(self):
        self.extraction_calls = 0
        self.inference_calls = 0

    def extract_query_facts(self, **kwargs):
        self.extraction_calls += 1
        return QueryExtractionFill(raw_time="现行")

    def infer_query(self, **kwargs):
        self.inference_calls += 1
        return QueryInference(jurisdiction="CN", version_kind="current")


def test_same_claim_source_shares_version_inference():
    claim = RawClaim(
        claim_id="c1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text="现行《星河数据治理法》第一条、第二条规定……",
        legal_mentions=[
            RawLegalMention(
                mention_id="m1", raw_title="星河数据治理法",
                article_raw="第一条", recognition_form="explicit",
                mention_span=(3, 10),
            ),
            RawLegalMention(
                mention_id="m2", raw_title="星河数据治理法",
                article_raw="第二条", recognition_form="explicit",
                mention_span=(3, 10),
            ),
        ],
    )
    context = build_document_context(RawClaimDocument(
        source_claim_doc_id="d", claims=[claim]
    ))
    resources = QueryResources(lexicon=LawLexicon([]))
    planner = _SharedSourcePlanner()

    first = build_initial_hypothesis(
        claim, resources, mention_id="m1", planner=planner,
        document_context=context,
    )
    second = build_initial_hypothesis(
        claim, resources, mention_id="m2", planner=planner,
        document_context=context,
    )

    assert planner.extraction_calls == 0
    assert planner.inference_calls == 1
    assert first.query_plan.version_hint == "current"
    assert second.query_plan.version_hint == "current"


def test_fuzzy_candidates_are_cached_per_unique_title(monkeypatch):
    import ccitecheck.query_construction.service as service_module

    calls = 0

    def fake_fuzzy(raw_title, titles):
        nonlocal calls
        calls += 1
        return ["中华人民共和国星河数据治理法"]

    monkeypatch.setattr(service_module, "fuzzy_title_candidates", fake_fuzzy)
    resources = QueryResources(lexicon=LawLexicon([]))

    assert resources.fuzzy_titles("星河数据治理法")
    assert resources.fuzzy_titles("星河数据治理法")
    assert calls == 1


def test_version_signal_is_not_applied_to_another_law_in_same_claim():
    text = "现行《公司法》第一条以及《民法典》第二条规定……"
    company_start = text.index("公司法")
    civil_start = text.index("民法典")
    claim = RawClaim(
        claim_id="c1",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        raw_text=text,
        legal_mentions=[
            RawLegalMention(
                mention_id="m1", raw_title="公司法", raw_time="现行",
                article_raw="第一条", recognition_form="explicit",
                mention_span=(company_start, company_start + len("公司法")),
            ),
            RawLegalMention(
                mention_id="m2", raw_title="民法典", raw_time="现行",
                article_raw="第二条", recognition_form="explicit",
                mention_span=(civil_start, civil_start + len("民法典")),
            ),
        ],
    )
    resources = QueryResources(lexicon=LawLexicon([
        LawLexiconEntry("公司法", "中华人民共和国公司法"),
        LawLexiconEntry("民法典", "中华人民共和国民法典"),
    ]))

    company = build_initial_hypothesis(claim, resources, mention_id="m1")
    civil = build_initial_hypothesis(claim, resources, mention_id="m2")

    assert company.query_plan.version_hint == "current"
    assert civil.query_plan.version_hint is None
