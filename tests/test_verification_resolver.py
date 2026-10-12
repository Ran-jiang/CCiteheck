from pathlib import Path

from ccitecheck.domain.citation import (
    ArticleRef,
    CaseCitationEntities,
    CaseRef,
    Claim,
    ClaimDocument,
    ClaimMeta,
    ClaimType,
    LegalSource,
    LegalSourceClaimEntities,
    UnresolvedLegalMention,
)
from ccitecheck.infrastructure.database import (
    connect,
    init_db,
    upsert_article,
    upsert_law,
)
from ccitecheck.application.verify_claims import verify_claim_document
from ccitecheck.verification.semantic import SemanticTransportError
from ccitecheck.output.summary import summarize_verification
from ccitecheck.domain.evidence import (
    ArticleEvidence,
    ArticleExcerpt,
    CaseLookupStatus,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from ccitecheck.domain.case_results import CaseVerificationResult
from ccitecheck.domain.queries import (
    RerankBatch,
    RerankDecision,
    RetrievalQueryPlan,
)
from ccitecheck.domain.statute_results import (
    LegalApplicationCheck,
    LegalApplicationReview,
    StatuteErrorCode,
    StatuteFinding,
)
from ccitecheck.retrieval.sources.pkulaw.client import (
    PkulawArticle,
    PkulawCaseRecord,
    PkulawLawRecord,
    PkulawMcpError,
    PkulawNotFoundError,
)
from ccitecheck.retrieval.sources import (
    LocalSQLiteSource,
    LookupRequest,
    LookupResult,
    PkulawFallbackSource,
)
from ccitecheck.retrieval.sources.base import LocationCandidateResult
from ccitecheck.orchestration.policies.retrieval import lookup_with_chain


class MissingArticleWithCandidateSource:
    def lookup(self, request):
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="fake pkulaw",
            status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
            metadata={"search_completed": True, "version_key": "2024-01-01", "version_confirmed": True},
        )
        evidence = ArticleEvidence(
            law_title=request.law_title,
            source_type="law",
            source_metadata={"version_key": "2024-01-01"},
            article_no=request.article_no,
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)

    def locate_candidates(self, request):
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="fake pkulaw locator",
            status=LookupStatus.RELEVANT_ARTICLES_FOUND,
        )
        evidence = ArticleEvidence(
            law_title=request.law_title,
            source_type="law",
            source_metadata={"version_key": "2024-01-01"},
            article_no="第二条",
            article_text="劳动者依法享有休息和休假的权利。",
            data_source=trace,
        )
        return LocationCandidateResult([evidence], trace)


class FailedStatuteSource:
    def lookup(self, request):
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="fake pkulaw",
            status=LookupStatus.SOURCE_ERROR,
            message="北大法宝鉴权失败（HTTP 401），请检查访问令牌、账户状态或剩余点数",
        )
        return LookupResult(trace.status, None, trace)


def test_statute_source_failure_message_reaches_frontend(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-source-error", source_doc_hash="sha256:error"),
        claims=[Claim(
            claim_id="cl_source_error",
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text="依据《示例法》第一条处理。",
            anchor_ids=["line00001"],
            entities=LegalSourceClaimEntities(legal_sources=[LegalSource(
                title="示例法",
                articles=[ArticleRef(article="第一条")],
            )]),
        )],
    )

    result = verify_claim_document(
        claim_doc,
        db_path,
        sources=[FailedStatuteSource()],
        include_cases=False,
    )

    check = result.statute_results[0]
    assert check.lookup_status == LookupStatus.SOURCE_ERROR
    assert "剩余点数" in check.message


class FailedCaseSearcher:
    def search_keyword(self, title, fulltext):
        raise PkulawMcpError("北大法宝鉴权失败（HTTP 401），请检查账户状态或剩余点数")

    def search_semantic(self, text):
        raise AssertionError("source failure should stop before semantic search")


def test_case_source_failure_message_reaches_frontend(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-case-error", source_doc_hash="sha256:case-error"),
        claims=[_case_claim("cl_case_error", "（2025）京01民终1号")],
    )

    result = verify_claim_document(
        claim_doc,
        db_path,
        case_searcher=FailedCaseSearcher(),
        include_statutes=False,
    )

    check = result.case_results[0]
    assert check.lookup_status == CaseLookupStatus.SOURCE_ERROR
    assert "数据源调用失败" in check.message
    assert "剩余点数" in check.message


def test_missing_article_with_confirmed_candidate_is_article_number_error(tmp_path: Path):
    text = "依据《示例法》第三条，劳动者依法享有休息和休假的权利。"
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-number", source_doc_hash="sha256:number"),
        claims=[Claim(
            claim_id="cl_number",
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text=text,
            anchor_ids=["line00001"],
            entities=LegalSourceClaimEntities(legal_sources=[LegalSource(
                title="示例法",
                articles=[ArticleRef(article="第三条")],
            )]),
        )],
    )

    result = verify_claim_document(
        claim_doc,
        tmp_path / "missing.sqlite",
        sources=[MissingArticleWithCandidateSource()],
        include_cases=False,
    )

    finding = result.statute_results[0].findings[0]
    assert finding.code == StatuteErrorCode.ARTICLE_NUMBER_ERROR
    assert finding.resolved_locator.article_no == "第二条"
    assert finding.revision is not None
    assert finding.suggestion == "条号引用错误，应为《示例法》第二条。"
    assert "《示例法》第二条" in finding.revision.revised_text


def test_nested_locator_mismatch_preserves_candidate_for_existing_repair_chain():
    from ccitecheck.verification.statutes.nested import resolve_nested_relations
    from ccitecheck.orchestration.scheduler import _CheckItem

    text = "《甲法》第一条规定：“依照《乙法》第二条处理。”"
    claim = Claim(
        claim_id="cl_nested_candidate", claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text, anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[]),
    )
    parent_ref = ArticleRef(article="第一条")
    child_start = text.index("《乙法》")
    child_ref = ArticleRef(article="第二条")
    parent = _CheckItem(
        claim, "甲法", "甲法", parent_ref, "第一条", None,
        citation_span=(0, 7), span_status="located",
    )
    child = _CheckItem(
        claim, "乙法", "乙法", child_ref, "第二条", None,
        citation_span=(child_start, child_start + 7), span_status="located",
    )

    def lookup(item, article_text):
        trace = SourceTrace(
            tier=SourceTier.LOCAL_SQLITE, source_name="test",
            status=LookupStatus.ARTICLE_FOUND,
        )
        evidence = ArticleEvidence(
            law_title=item.law_title, source_type="law",
            article_no=item.article_no, article_text=article_text, data_source=trace,
        )
        return LookupResult(LookupStatus.ARTICLE_FOUND, evidence, trace), [trace]

    lookups = {
        parent.lookup_key: lookup(parent, "依照乙法第二百一十一条处理。"),
        child.lookup_key: lookup(child, "当前第二条是其他规则。"),
    }

    resolve_nested_relations([parent, child], lookups)

    assert child.relation_status == "locator_mismatch"
    assert child.relation_candidate_article_no == "第二百一十一条"
    assert child.reference_role == "nested"


def test_nested_relation_matches_parent_article_set_without_llm():
    from ccitecheck.verification.statutes.nested import resolve_nested_relations
    from ccitecheck.orchestration.scheduler import _CheckItem

    text = "《甲法》第一条规定：“依照《乙法》第二条处理。”"
    claim = Claim(
        claim_id="cl_nested_no_checker", claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text, anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[]),
    )
    child_start = text.index("《乙法》")
    parent = _CheckItem(
        claim, "甲法", "甲法",
        ArticleRef(article="第一条"), "第一条", None,
        citation_span=(0, 7), span_status="located",
    )
    child = _CheckItem(
        claim, "乙法", "乙法",
        ArticleRef(article="第二条"), "第二条", None,
        citation_span=(child_start, child_start + 7), span_status="located",
    )
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE, source_name="test",
        status=LookupStatus.ARTICLE_FOUND,
    )
    lookups = {
        item.lookup_key: (LookupResult(
            LookupStatus.ARTICLE_FOUND,
            ArticleEvidence(
                law_title=item.law_title, source_type="law", article_no=item.article_no,
                article_text=text_value, data_source=trace,
            ), trace,
        ), [trace])
        for item, text_value in (
            (parent, "依照乙法第二条处理。"),
            (child, "被援引规则。"),
        )
    }

    resolve_nested_relations([parent, child], lookups)

    assert child.relation_status == "confirmed"
    assert child.relation_candidate_article_no == "第二条"


def test_internal_reference_is_confirmed_from_parent_authority(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        parent_id = upsert_law(conn, {
            "title": "最高人民法院关于适用《中华人民共和国民事诉讼法》的解释",
            "source_type": "judicial_interpretation", "status": "has_articles",
        })
        child_id = upsert_law(conn, {
            "title": "中华人民共和国民事诉讼法",
            "source_type": "law", "status": "has_articles",
        })
        upsert_article(conn, parent_id, {
            "article_no": "第二百八十六条",
            "text": "人民法院受理公益诉讼案件，不影响同一侵权行为的受害人根据民事诉讼法第一百二十二条规定提起诉讼。",
            "version_status": "effective", "source_name": "本地库",
        })
        upsert_article(conn, child_id, {
            "article_no": "第一百二十二条", "text": "起诉必须符合下列条件。",
            "version_status": "effective", "source_name": "本地库",
        })
    text = (
        "《最高人民法院关于适用《中华人民共和国民事诉讼法》的解释》"
        "第二百八十六条规定：“人民法院受理公益诉讼案件，不影响受害人根据"
        "《中华人民共和国民事诉讼法》第一百二十二条规定提起诉讼。”"
    )
    claim = Claim(
        claim_id="cl_nested", claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text, anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[
            LegalSource(
                title="最高人民法院关于适用《中华人民共和国民事诉讼法》的解释",

                articles=[ArticleRef(article="第二百八十六条")],
            ),
            LegalSource(
                title="中华人民共和国民事诉讼法",
                articles=[ArticleRef(article="第一百二十二条")],
            ),
        ]),
    )
    result = verify_claim_document(ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="d", source_doc_hash="h", source_file="x"),
        claims=[claim],
    ), db_path, sources=[LocalSQLiteSource(db_path)])

    parent, child = result.statute_results
    assert child.reference_role == "nested"
    assert child.parent_check_id == parent.check_id
    assert child.relation_status == "confirmed"
    assert child.outcome == "pass"


def test_parallel_legal_basis_is_not_internal_reference(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        for title, article in (("甲法", "第一条"), ("乙法", "第二条")):
            law_id = upsert_law(conn, {"title": title, "source_type": "law", "status": "has_articles"})
            upsert_article(conn, law_id, {
                "article_no": article, "text": "独立法律依据。",
                "version_status": "effective", "source_name": "本地库",
            })
    text = "根据《甲法》第一条，并根据《乙法》第二条处理。"
    claim = Claim(
        claim_id="cl_parallel", claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text, anchor_ids=["line1"],
        entities=LegalSourceClaimEntities(legal_sources=[
            LegalSource(title="甲法", articles=[ArticleRef(article="第一条")]),
            LegalSource(title="乙法", articles=[ArticleRef(article="第二条")]),
        ]),
    )
    result = verify_claim_document(ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="d", source_doc_hash="h", source_file="x"), claims=[claim],
    ), db_path, sources=[LocalSQLiteSource(db_path)])
    assert all(item.reference_role == "direct" for item in result.statute_results)


def test_frontend_verification_json_includes_local_article(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn,
            {
                "title": "中华人民共和国劳动合同法",
                "source_type": "law",
                "status": "has_articles",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第三十七条",
                "text": "劳动者提前三十日以书面形式通知用人单位，可以解除劳动合同。",
                "version_label": "现行有效",
                "version_status": "effective",
                "source_name": "国家法律法规数据库",
                "source_url": "https://flk.npc.gov.cn/",
                "source_fetched_at": "2026-07-09T00:00:00+08:00",
            },
        )

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text="依据《劳动合同法》第三十七条，劳动者可以解除劳动合同。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="劳动合同法",

                            articles=[ArticleRef(article="第三十七条")],
                        )
                    ]
                ),
            )
        ],
    )

    frontend_doc = verify_claim_document(claim_doc, db_path)

    assert len(frontend_doc.statute_results) == 1
    check = frontend_doc.statute_results[0]
    assert check.lookup_status == LookupStatus.ARTICLE_FOUND
    assert check.evidence is not None
    assert check.evidence.law_title == "中华人民共和国劳动合同法"
    assert "提前三十日" in check.evidence.article_text
    assert check.source_attempts[0].source_name == "国家法律法规数据库"


def test_unresolved_bare_law_is_omitted_from_verification(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    text = "依照城市房地产管理法第38条处理。"
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-unresolved",
            source_doc_hash="sha256:unresolved",
            source_file="test.docx",
        ),
        claims=[Claim(
            claim_id="cl_unresolved",
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text=text,
            anchor_ids=["line00001"],
            entities=LegalSourceClaimEntities(unresolved_legal_mentions=[
                UnresolvedLegalMention(
                    raw_text="城市房地产管理法",
                    articles=[ArticleRef(article="第38条")],
                    resolution_anchor_span=(9, 10),
                )
            ]),
        )],
    )

    frontend_doc = verify_claim_document(claim_doc, db_path, sources=[])

    assert frontend_doc.statute_results == []


class FakeSemanticChecker:
    def plan_related(self, *, raw_text, raw_title, raw_time, target_name):
        return RetrievalQueryPlan(
            route="statute_related",
            target_name=target_name,
            query_text=raw_text,
        )

    def rerank_related(self, query_text, candidates):
        return RerankBatch(results=[
            RerankDecision(
                candidate_id=item.candidate_id,
                relevant=True,
                score=1.0,
            )
            for item in candidates
        ])

    def compare_application(self, original_text, authorities):
        return LegalApplicationCheck(
            verdict="review",
            reviews=[LegalApplicationReview(
                error_type="meaning_distorted",
                summary="文书未明示被告不履行或履行不符合约定",
                suggestion="核实并补充违约事实。",
            )],
        )


class RejectAllSemanticChecker(FakeSemanticChecker):
    def rerank_related(self, query_text, candidates):
        return RerankBatch(results=[
            RerankDecision(candidate_id=item.candidate_id, relevant=False, score=0.01)
            for item in candidates
        ])

    def compare_application(self, original_text, authorities):
        raise AssertionError("all-rejected related candidates must skip comparison")


def test_application_assessment_is_added_when_checker_is_configured(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn, {"title": "中华人民共和国民法典", "source_type": "law"}
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第五百七十七条",
                "text": "当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担违约责任。",
            },
        )

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text="依据《民法典》第五百七十七条，被告应当承担违约责任。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="民法典",

                            articles=[ArticleRef(article="第五百七十七条")],
                        )
                    ]
                ),
            )
        ],
    )

    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        semantic_checker=FakeSemanticChecker(),
    )

    check = frontend_doc.statute_results[0]
    comparison = check.application_check
    assert comparison.verdict == "review"
    assert comparison.reviews[0].error_type == "meaning_distorted"
    assert not hasattr(check, "meaning_check")


def test_unnumbered_citation_retrieves_related_local_articles(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn,
            {"title": "中华人民共和国网络安全法", "source_type": "law"},
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第一条",
                "text": "为了保障网络安全，维护国家安全和社会公共利益，保护公民、法人和其他组织的合法权益。",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第二条",
                "text": "本法适用于境内网络的建设、运营、维护和使用。",
            },
        )

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text="根据《网络安全法》，保障公民、法人合法权益，维护国家安全和公共利益。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="网络安全法",

                        )
                    ]
                ),
            )
        ],
    )

    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        semantic_checker=FakeSemanticChecker(),
    )

    check = frontend_doc.statute_results[0]
    assert check.lookup_status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert check.cited_locators == []
    assert check.evidence.related_articles[0].article_no == "第一条"
    # Planner 已提炼出具体法律问题时，相关条文进入适用侧 LLM 比对。
    assert check.application_check is not None
    assert check.outcome == "review"


def test_unnumbered_all_rejected_candidates_pass_existence_only(tmp_path: Path):
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="existence", source_doc_hash="sha256:existence"),
        claims=[Claim(
            claim_id="cl_existence",
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text="根据《中华人民共和国刑法》，本案应依法处理。",
            anchor_ids=["line1"],
            entities=LegalSourceClaimEntities(legal_sources=[LegalSource(
                title="中华人民共和国刑法",
            )]),
        )],
    )
    result = verify_claim_document(
        claim_doc,
        tmp_path / "laws.sqlite",
        sources=[RelatedCandidateSourceForResolver()],
        semantic_checker=RejectAllSemanticChecker(),
        include_cases=False,
    )
    check = result.statute_results[0]
    assert check.lookup_status == LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
    assert check.application_check is None
    assert check.outcome == "pass"


class RelatedCandidateSourceForResolver:
    def lookup(self, request):
        trace = SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="fake recall",
            status=LookupStatus.RELEVANT_ARTICLES_FOUND,
        )
        evidence = ArticleEvidence(
            law_title=request.law_title,
            related_articles=[ArticleExcerpt(
                article_no="第一条",
                article_text="为了惩罚犯罪，保护人民，根据宪法，制定本法。",
                relevance_score=1.0,
            )],
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)


class FakeLawListClient:
    def get_law_list(self, title="", fulltext=""):
        return [
            PkulawLawRecord(title="中华人民共和国国家安全法", timeliness=["现行有效"])
        ]

    def search_law_articles(self, text):
        return []


class FakeCanonicalTitleRetryClient:
    canonical_title = "最高人民法院、最高人民检察院关于办理危害计算机信息系统安全刑事案件应用法律若干问题的解释"

    def __init__(self):
        self.article_titles = []

    def get_article(self, title, article_no):
        self.article_titles.append(title)
        raise PkulawNotFoundError("未找到数据")

    def search_law_articles_for_article(self, title, article_no):
        return [
            PkulawArticle(
                title=self.canonical_title,
                article_no=article_no,
                article_text="非法获取计算机信息系统数据，具有法定情形的，应当认定为情节严重。",
                url="https://www.pkulaw.com/lar/example.html",
            )
        ]

    def get_law_list(self, title="", fulltext=""):
        return [PkulawLawRecord(title=self.canonical_title)]


def test_pkulaw_article_uses_semantic_fallback_after_exact_miss():
    client = FakeCanonicalTitleRetryClient()
    result = PkulawFallbackSource(client).lookup(
        LookupRequest(
            law_title="关于办理危害计算机信息系统安全刑事案件应用法律若干问题的解释",
            article_no="第一条",
        )
    )

    assert result.status == LookupStatus.ARTICLE_FOUND
    assert result.evidence.article_text.startswith("非法获取")
    assert client.article_titles[-1] != client.canonical_title
    assert any(
        attempt["service"] == "law_semantic_exact"
        for attempt in result.trace.metadata["route_attempts"]
    )


class FakePrefixRecallClient:
    """模拟法宝：精确错名查不到，但前缀能召回正确法名。"""
    correct = "互联网论坛社区服务管理规定"

    def get_article(self, title, article_no):
        if title == self.correct:
            return PkulawArticle(
                title=self.correct,
                article_no=article_no,
                article_text="互联网论坛社区服务提供者应当落实主体责任……",
                url="https://pkulaw.com/chl/example.html",
            )
        raise PkulawNotFoundError("未找到数据")

    def search_law_articles_for_article(self, title, article_no):
        return []

    def get_law_list(self, title="", fulltext=""):
        # 精确错名或全文查不到；仅当查询是正确法名的前缀时命中
        if title and self.correct.startswith(title):
            return [PkulawLawRecord(title=self.correct)]
        raise PkulawNotFoundError("未找到数据")


def test_pkulaw_not_found_recalls_similar_name_for_suggestion():
    from ccitecheck.verification.statutes import assess_statute

    request = LookupRequest(
        law_title="互联网论坛服务管理规定",
        article_no="第五条",
    )
    result = PkulawFallbackSource(FakePrefixRecallClient()).lookup(request)

    assert result.status == LookupStatus.LAW_NOT_FOUND
    assert "互联网论坛社区服务管理规定" in result.trace.metadata["candidate_titles"]

    # Retrieval 只返回候选名称；新法名必须先形成新 hypothesis 再正式取证。
    assert result.evidence is None

    findings = assess_statute(
        request.law_title, request.article_no, result, [result.trace], []
    )
    assert findings
    assert findings[0].code == StatuteErrorCode.SOURCE_NOT_FOUND


def test_pkulaw_unnumbered_lookup_reports_tool_text_limit():
    result = PkulawFallbackSource(FakeLawListClient()).lookup(
        LookupRequest(
            law_title="中华人民共和国国家安全法",
            context_text="维护国家安全和公共利益",
        )
    )

    assert result.status == LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
    assert "未取得条文全文" in result.trace.message


class FakeSemanticLawClient(FakeLawListClient):
    def search_law_articles(self, text):
        assert text.startswith("在《中华人民共和国国家安全法》中检索")
        return [
            PkulawArticle(
                title="中华人民共和国国家安全法",
                article_no="第三条",
                article_text="国家安全工作应当坚持总体国家安全观。",
                url="https://example.com/law/3",
            )
        ]


def test_pkulaw_unnumbered_lookup_uses_semantic_article_service_first():
    result = PkulawFallbackSource(FakeSemanticLawClient()).lookup(
        LookupRequest(
            law_title="中华人民共和国国家安全法",
            context_text="国家安全工作应当坚持总体国家安全观。",
        )
    )

    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert result.evidence.related_articles[0].article_no == "第三条"
    assert result.trace.metadata["retrieval_method"] == "pkulaw_law_semantic"


def test_pkulaw_unnumbered_lookup_without_credentials_is_nonfatal(monkeypatch):
    monkeypatch.setenv("PKULAW_ACCESS_TOKEN", "")

    result = PkulawFallbackSource().lookup(
        LookupRequest(
            law_title="虚构测试法",
            context_text="测试引用表述",
        )
    )

    assert result.status == LookupStatus.SOURCE_NOT_CONFIGURED


class FakeArticleSource:
    def lookup(self, request: LookupRequest) -> LookupResult:
        trace = SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="fake pkulaw",
            status=LookupStatus.ARTICLE_FOUND,
        )
        return LookupResult(
            LookupStatus.ARTICLE_FOUND,
            ArticleEvidence(
                law_title="中华人民共和国民法典",
                source_type="law",
                article_no=request.article_no,
                article_text="第五百七十七条　当事人一方不履行合同义务。",
                data_source=trace,
            ),
            trace,
        )


def test_local_catalog_without_article_continues_to_next_source(tmp_path: Path):
    from ccitecheck.infrastructure.database import seed_common_laws

    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    seed_common_laws(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text="依据《民法典》第五百七十七条，被告应当承担违约责任。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="民法典",

                            articles=[ArticleRef(article="第五百七十七条")],
                        )
                    ]
                ),
            )
        ],
    )

    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        sources=[
            LocalSQLiteSource(db_path),
            FakeArticleSource(),
        ],
    )

    check = frontend_doc.statute_results[0]
    assert check.lookup_status == LookupStatus.ARTICLE_FOUND
    assert len(check.source_attempts) == 2
    assert "不履行合同义务" in check.evidence.article_text


def test_legacy_local_mcp_url_is_replaced_by_exact_article_url():
    request = LookupRequest(
        law_title="中华人民共和国商标法",
        article_no="第十三条",
    )

    class Source:
        def __init__(self, tier, url, text):
            self.tier, self.url, self.text = tier, url, text

        def lookup(self, request):
            trace = SourceTrace(
                tier=self.tier,
                source_name="local" if self.tier == SourceTier.LOCAL_SQLITE else "pkulaw",
                source_url=self.url,
                status=LookupStatus.ARTICLE_FOUND,
            )
            return LookupResult(
                LookupStatus.ARTICLE_FOUND,
                ArticleEvidence(
                    law_title=request.law_title,
                    source_type="law",
                    article_no=request.article_no,
                    article_text=self.text,
                    source_metadata={"version_key": "2024-01-01"},
                    data_source=trace,
                ),
                trace,
            )

    result, attempts = lookup_with_chain(
        [
            Source(
                SourceTier.LOCAL_SQLITE,
                "[北大法宝](https://www.pkulaw.com/lar/dead.html?way=mcp)",
                "本地条文",
            ),
            Source(
                SourceTier.PKULAW_FALLBACK,
                "https://pkulaw.com/chl/current.html",
                "精确法条",
            ),
        ],
        request,
    )

    assert len(attempts) == 2
    assert result.evidence.article_text == "本地条文"
    assert result.evidence.data_source.tier == SourceTier.LOCAL_SQLITE
    assert result.trace.source_url == "https://pkulaw.com/chl/current.html"


def test_legacy_local_mcp_url_is_hidden_when_remote_repair_fails():
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE,
        source_name="local",
        source_url="https://www.pkulaw.com/lar/dead.html?way=mcp",
        status=LookupStatus.ARTICLE_FOUND,
    )
    local = LookupResult(
        LookupStatus.ARTICLE_FOUND,
        ArticleEvidence(
            law_title="中华人民共和国商标法",
            source_type="law",
            article_no="第十三条",
            article_text="仍可用于核查的本地条文",
            data_source=trace,
        ),
        trace,
    )

    class FixedSource:
        def __init__(self, result): self.result = result
        def lookup(self, request): return self.result

    error_trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="pkulaw",
        status=LookupStatus.SOURCE_ERROR,
    )
    result, attempts = lookup_with_chain(
        [FixedSource(local), FixedSource(LookupResult(LookupStatus.SOURCE_ERROR, None, error_trace))],
        LookupRequest(law_title="中华人民共和国商标法", article_no="第十三条"),
    )

    assert len(attempts) == 4
    assert [attempt.status for attempt in attempts[-3:]] == [
        LookupStatus.SOURCE_ERROR,
    ] * 3
    assert result.evidence.article_text == "仍可用于核查的本地条文"
    assert result.trace.source_url is None


class FakeCaseSearcher:
    def __init__(self, cases: list[PkulawCaseRecord]):
        self.cases = cases

    def search_keyword(self, title: str, fulltext: str):
        normalized = fulltext.replace("（", "(").replace("）", ")")
        return [case for case in self.cases if case.case_number == normalized]

    def search_semantic(self, text: str):
        return []


def _case_claim(claim_id: str, case_number: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=ClaimType.CASE_CITATION,
        text=f"参见{case_number}民事判决。",
        anchor_ids=["line00001"],
        entities=CaseCitationEntities(
            case_refs=[
                CaseRef(

                    case_number=case_number,
                )
            ]
        ),
    )


def test_case_numbers_verified_and_flagged_by_searcher(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            _case_claim("cl_00001", "（2024）浙0114破1-6号之二"),
            _case_claim("cl_00002", "（2099）虚构民终9999号"),
        ],
    )
    searcher = FakeCaseSearcher(
        [
            PkulawCaseRecord(
                gid="08df102e7c10f206",
                case_number="(2024)浙0114破1-6号之二",
                court="浙江省杭州市钱塘区人民法院",
                title="指导性案例252号：某执行实施案",
                last_instance_date="2024.06.18",
                url="https://www.pkulaw.com/pfnl/08df102e7c10f206.html",
            )
        ]
    )

    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        case_searcher=searcher,
    )

    assert not frontend_doc.statute_results
    verified, flagged = frontend_doc.case_results
    assert verified.lookup_status == CaseLookupStatus.VERIFIED
    assert verified.evidence.court == "浙江省杭州市钱塘区人民法院"
    assert verified.evidence.url.endswith(".html")
    assert flagged.lookup_status == CaseLookupStatus.NOT_FOUND
    assert flagged.evidence is None


class RoutingCaseSearcher:
    def __init__(self):
        self.keyword_calls = []
        self.semantic_calls = []

    def search_keyword(self, title, fulltext):
        self.keyword_calls.append((title, fulltext))
        return [PkulawCaseRecord(title="不相关案例", url="https://example.com/other")]

    def search_semantic(self, text):
        self.semantic_calls.append(text)
        return [
            PkulawCaseRecord(
                title="指导性案例262号：某平台纠纷案",
                case_number="（2024）最高法民终262号",
                gid="case-262",
                court="最高人民法院",
                url="https://example.com/case/262",
            )
        ]


def test_case_without_number_uses_keyword_then_semantic_search(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.CASE_CITATION,
                text="最高人民法院在指导案例262号中明确了平台责任。",
                context_text="最高人民法院在指导案例262号中明确了平台责任。",
                anchor_ids=["line00001"],
                entities=CaseCitationEntities(
                    case_refs=[
                        CaseRef(

                            case_name="指导案例262号",
                            court="最高人民法院",
                        )
                    ]
                ),
            )
        ],
    )
    searcher = RoutingCaseSearcher()

    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        case_searcher=searcher,
    )

    check = frontend_doc.case_results[0]
    assert check.lookup_status == CaseLookupStatus.VERIFIED
    assert check.evidence.case_number == "（2024）最高法民终262号"
    assert len(check.source_attempts) == 2
    assert searcher.keyword_calls[0][0] == "指导性案例262号"
    assert searcher.semantic_calls


def test_party_style_case_name_is_cleaned_and_sent_as_party_keywords(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[Claim(
            claim_id="cl_00001",
            claim_type=ClaimType.CASE_CITATION,
            text="在庄羽诉郭敬明案中，法院讨论了作品独创性。",
            anchor_ids=["line00001"],
            entities=CaseCitationEntities(case_refs=[CaseRef(

                case_name="在庄羽诉郭敬明案",
            )]),
        )],
    )

    class PartySearcher:
        calls = []
        def search_keyword(self, title, fulltext):
            self.calls.append((title, fulltext))
            if title:
                return []
            return [PkulawCaseRecord(
                title="庄羽与郭敬明等侵犯著作权纠纷上诉案",
                case_number="(2005)高民终字第539号",
            )]
        def search_semantic(self, text):
            return []

    searcher = PartySearcher()
    verify_claim_document(claim_doc, db_path, case_searcher=searcher)
    assert searcher.calls == [("庄羽诉郭敬明案", ""), ("", "庄羽 郭敬明")]


def test_case_name_containment_requires_manual_review(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.CASE_CITATION,
                text="甲公司诉乙公司合同纠纷案具有参考意义。",
                context_text="甲公司诉乙公司合同纠纷案具有参考意义。",
                anchor_ids=["line00001"],
                entities=CaseCitationEntities(
                    case_refs=[
                        CaseRef(

                            case_name="甲公司诉乙公司合同纠纷案",
                        )
                    ]
                ),
            )
        ],
    )

    class ContainmentSearcher:
        def search_keyword(self, title, fulltext):
            return [PkulawCaseRecord(title="甲公司诉乙公司合同纠纷案再审审查案")]

        def search_semantic(self, text):
            return []

    check = verify_claim_document(
        claim_doc, db_path, case_searcher=ContainmentSearcher()
    ).case_results[0]
    assert check.lookup_status == CaseLookupStatus.MANUAL_REVIEW
    assert [candidate.title for candidate in check.candidate_cases] == [
        "甲公司诉乙公司合同纠纷案再审审查案"
    ]


def test_match_law_record_accepts_promulgation_notice_title():
    from ccitecheck.retrieval.sources.pkulaw.client import PkulawLawRecord
    from ccitecheck.query_construction.matching import match_law_record

    records = [
        PkulawLawRecord(
            title="中国互联网金融协会关于举办“《常见类型移动互联网应用程序必要个人信息范围规定》政策解读”培训班的通知"
        ),
        PkulawLawRecord(
            title="国家互联网信息办公室秘书局等关于印发《常见类型移动互联网应用程序必要个人信息范围规定》的通知"
        ),
    ]
    matched = match_law_record(
        "常见类型移动互联网应用程序必要个人信息范围规定", records
    )
    assert matched is records[1]
    assert (
        match_law_record("常见类型移动互联网应用程序必要个人信息范围规定", records[:1])
        is None
    )


def test_match_law_record_accepts_fullwidth_angle_brackets_in_citation():
    from ccitecheck.retrieval.sources.pkulaw.client import PkulawLawRecord
    from ccitecheck.query_construction.matching import match_law_record

    record = PkulawLawRecord(
        title="最高人民法院关于适用《中华人民共和国民法典》合同编通则若干问题的解释"
    )

    matched = match_law_record(
        "最高人民法院关于适用＜中华人民共和国民法典＞合同编通则若干问题的解释",
        [record],
    )

    assert matched is record


def test_match_law_record_ignores_supreme_court_issuer_prefix():
    from ccitecheck.query_construction.matching import (
        equivalent_law_titles,
        match_law_record,
    )

    short = "关于审理未成年人刑事案件具体应用法律若干问题的解释"
    full = f"最高人民法院{short}"
    record = PkulawLawRecord(title=full)

    assert equivalent_law_titles(f"《{short}》", full)
    assert match_law_record(short, [record]) is record


def test_match_law_record_prefers_explicit_current_version():
    from ccitecheck.retrieval.sources.pkulaw.client import PkulawLawRecord
    from ccitecheck.query_construction.matching import match_law_record

    records = [
        PkulawLawRecord(title="示例解释", timeliness=["已被修改"]),
        PkulawLawRecord(title="示例解释(2020修正)", timeliness=["现行有效"]),
    ]
    matched = match_law_record("示例解释（2020修正）", records)
    assert matched is records[1]


def test_match_law_record_bare_name_resolves_to_current_reenacted_version():
    """裸名引用（如《国家安全法》）在旧版废止、新版现行同名时应命中现行版，
    避免把重新制定后仍施行的法误判为废止。"""
    from ccitecheck.retrieval.sources.pkulaw.client import PkulawLawRecord
    from ccitecheck.query_construction.matching import match_law_record

    records = [
        PkulawLawRecord(title="中华人民共和国国家安全法", timeliness=["废止或失效"]),
        PkulawLawRecord(title="中华人民共和国国家安全法(2015)", timeliness=["现行有效"]),
        PkulawLawRecord(title="中华人民共和国国家安全法(2009修正)", timeliness=["废止或失效"]),
        PkulawLawRecord(title="中华人民共和国国家安全法实施细则", timeliness=["废止或失效"]),
    ]
    matched = match_law_record("中华人民共和国国家安全法", records)
    assert matched is records[1]
    assert matched.timeliness == ["现行有效"]


def test_match_law_record_bare_name_matches_year_suffixed_single_article():
    """get_article 常返回带纯年份后缀的版本标题（如"…国家安全法(2015)"），
    裸名引用校验单条返回时也应认定为同一部法。"""
    from ccitecheck.retrieval.sources.pkulaw.client import PkulawArticle
    from ccitecheck.query_construction.matching import match_law_record

    article = PkulawArticle(
        title="中华人民共和国国家安全法(2015)",
        article_no="第二条",
        article_text="国家安全是指……",
    )
    assert match_law_record("中华人民共和国国家安全法", [article]) is article


def _simple_claim(claim_id: str, text: str, title: str, article: str) -> Claim:
    return Claim(
        claim_id=claim_id,
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title=title,

                    articles=[ArticleRef(article=article)],
                )
            ]
        ),
    )


class CountingSource:
    """记录 lookup 调用次数的假法条源。"""

    def __init__(self):
        self.calls = []

    def lookup(self, request: LookupRequest) -> LookupResult:
        self.calls.append((request.law_title, request.article_no))
        trace = SourceTrace(
            tier=SourceTier.LOCAL_SQLITE,
            source_name="counting",
            status=LookupStatus.ARTICLE_FOUND,
        )
        evidence = ArticleEvidence(
            law_title=request.law_title,
            source_type="law",
            article_no=request.article_no,
            article_text="条文",
            data_source=trace,
        )
        return LookupResult(LookupStatus.ARTICLE_FOUND, evidence, trace)


class FailOncePerQuoteChecker:
    def __init__(self):
        self.calls = {}

    def compare_application(self, original_text, authorities):
        count = self.calls.get(original_text, 0) + 1
        self.calls[original_text] = count
        if count == 1:
            raise SemanticTransportError("temporary EOF", "transport_error")
        return LegalApplicationCheck(verdict="pass")


class GroupApplicationChecker:
    def __init__(self):
        self.calls = []

    def compare_application(self, original_text, authorities):
        from ccitecheck.domain.statute_results import (
            LegalApplicationCheck,
            LegalApplicationReview,
        )
        self.calls.append((original_text, authorities))
        return LegalApplicationCheck(
            verdict="review",
            reviews=[LegalApplicationReview(
                error_type="rule_fact_mismatch",
                summary="两项结论与两部法律之间没有明确对应关系。",
                suggestion="请逐项对应事实、结论与法条。",
                related_sources=[item.cited_source for item in authorities],
            )],
        )


class FailedApplicationChecker:
    def compare_application(self, original_text, authorities):
        raise SemanticTransportError(
            "transport_error: upstream connection reset", "transport_error"
        )


def test_legal_application_is_grouped_and_can_only_be_review(tmp_path: Path):
    checker = GroupApplicationChecker()
    claim = Claim(
        claim_id="cl_application_group",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text="依据《甲法》第一条和《乙法》第二条，应支持全部请求。",
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(legal_sources=[
            LegalSource(title="甲法", articles=[ArticleRef(article="第一条")]),
            LegalSource(title="乙法", articles=[ArticleRef(article="第二条")]),
        ]),
    )
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
            document_text="全文第一段。\n依据《甲法》第一条和《乙法》第二条，应支持全部请求。",
            claims=[claim],
        ),
        tmp_path / "missing.sqlite",
        sources=[CountingSource()],
        semantic_checker=checker,
        include_cases=False,
    )

    assert len(checker.calls) == 1
    assert checker.calls[0][0] == "依据《甲法》第一条和《乙法》第二条，应支持全部请求。"
    assert len(checker.calls[0][1]) == 2
    assert [item.outcome for item in result.statute_results] == ["review", "pass"]
    assert result.statute_results[0].findings == []
    assert result.statute_results[0].application_check.verdict == "review"
    assert result.statute_results[0].application_check.reviews[0].review_level == "待核查"
    summary = summarize_verification(result)
    assert summary.reviews == 1
    assert summary.issues == 0


def test_legal_application_failure_hides_transport_detail(tmp_path: Path):
    claim = _simple_claim(
        "cl_application_failure",
        "依据《个人信息保护法》第五条处理。",
        "个人信息保护法",
        "第五条",
    )
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
            document_text="不应发送给模型的全文。\n依据《个人信息保护法》第五条处理。",
            claims=[claim],
        ),
        tmp_path / "missing.sqlite",
        sources=[CountingSource()],
        semantic_checker=FailedApplicationChecker(),
        include_cases=False,
    ).statute_results[0]

    assert result.message == "模型服务暂时不可用"
    assert result.application_check.notes == "模型服务暂时不可用"
    assert "transport" not in result.model_dump_json().lower()


def test_semantic_salvage_retries_in_submission_order_and_honors_cap(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("QWEN_SEMANTIC_WORKERS", "1")
    monkeypatch.setenv("QWEN_SALVAGE_MAX", "1")
    checker = FailOncePerQuoteChecker()
    claims = [
        _simple_claim("cl_00001", "第一次引用第五条。", "个人信息保护法", "第五条"),
        _simple_claim("cl_00002", "第二次引用第六条。", "个人信息保护法", "第六条"),
    ]
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
            claims=claims,
        ),
        tmp_path / "missing.sqlite",
        sources=[CountingSource()],
        semantic_checker=checker,
        include_cases=False,
    )
    comparisons = [reference.application_check for reference in result.statute_results]
    assert comparisons[0].execution_status == "completed"
    assert comparisons[1].execution_status == "llm_error"
    assert comparisons[1].retryable is True
    assert list(checker.calls.values()) == [2, 1]


def test_multiple_statute_references_share_one_group(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim = Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text="依照《商标法》第十三条第一款、第三款，《解释》第九条、第十条，判决如下。",
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(legal_sources=[
            LegalSource(
                title="商标法",

                articles=[ArticleRef(article="第十三条", paragraphs=["第一款", "第三款"])],
            ),
            LegalSource(
                title="解释",

                articles=[ArticleRef(article="第九条"), ArticleRef(article="第十条")],
            ),
        ]),
    )
    result = verify_claim_document(
        ClaimDocument(
            claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
            claims=[claim],
        ),
        db_path,
        sources=[CountingSource()],
    )
    assert len({reference.card_id for reference in result.statute_results}) == 1
    # 同一条的不同款在识别层按引用出现次数保留，检索层再复用同一条证据。
    assert len(result.statute_results) == 4
    assert [
        result.statute_results[index].cited_locators[0].paragraph_no
        for index in (0, 1)
    ] == ["第一款", "第三款"]
    summary = summarize_verification(result)
    assert summary.card_total == 1
    assert summary.reference_total == 4
    assert summary.total == 4
    result.case_results.append(CaseVerificationResult(
        check_id="cc_00001",
        display_group_id=result.statute_results[0].display_group_id,
        claim_id="cl_00001",
        claim_text=claim.text,
        lookup_status=CaseLookupStatus.VERIFIED,
        outcome="pass",
    ))
    mixed_summary = summarize_verification(result)
    assert mixed_summary.card_total == 1
    assert mixed_summary.reference_total == 5


def test_duplicate_citations_share_one_lookup(tmp_path: Path):
    """同一（法名, 条号）在多个 claim 中重复引用时只查一次。"""
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            _simple_claim("cl_00001", "第一次引用第五条。", "个人信息保护法", "第五条"),
            _simple_claim("cl_00002", "第二次引用第五条。", "个人信息保护法", "第五条"),
            _simple_claim("cl_00003", "引用第六条。", "个人信息保护法", "第六条"),
        ],
    )
    source = CountingSource()
    frontend_doc = verify_claim_document(claim_doc, db_path, sources=[source])
    # 3 条 check 全部产出，但底层只发生 2 次查询（第五条去重）
    assert len(frontend_doc.statute_results) == 3
    assert len(source.calls) == 2
    assert frontend_doc.statute_results[0].evidence.article_text == "条文"
    assert frontend_doc.statute_results[1].evidence.article_text == "条文"


class CountingCaseSearcher:
    def __init__(self):
        self.calls = 0

    def search_keyword(self, title: str, fulltext: str):
        self.calls += 1
        if fulltext == "（2020）京01民终1号":
            return [PkulawCaseRecord(
                gid="g1",
                case_number="（2020）京01民终1号",
                court="北京市第一中级人民法院",
                title="某案",
            )]
        return []

    def search_semantic(self, text: str):
        return []


def test_case_claims_use_exact_then_semantic_route(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)

    def case_claim(claim_id: str, number: str) -> Claim:
        return Claim(
            claim_id=claim_id,
            claim_type=ClaimType.CASE_CITATION,
            text=f"参见{number}判决。",
            anchor_ids=["line00001"],
            entities=CaseCitationEntities(

                case_refs=[
                    CaseRef(

                        case_number=number,
                    )
                ],
            ),
        )

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            case_claim("cl_00001", "（2020）京01民终1号"),
            case_claim("cl_00002", "（2021）沪02民终2号"),
        ],
    )
    searcher = CountingCaseSearcher()
    frontend_doc = verify_claim_document(
        claim_doc, db_path, case_searcher=searcher, include_statutes=False
    )
    assert searcher.calls == 2
    assert len(frontend_doc.case_results) == 2
    statuses = {check.lookup_status for check in frontend_doc.case_results}
    assert statuses == {CaseLookupStatus.VERIFIED, CaseLookupStatus.NOT_FOUND}


def test_deterministic_findings_skip_meaning_llm(tmp_path: Path):
    """确定性规则已有结论时不再调用语义核查。"""
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)

    class RepealedSource:
        def lookup(self, request: LookupRequest) -> LookupResult:
            trace = SourceTrace(
                tier=SourceTier.PKULAW_FALLBACK,
                source_name="fake",
                status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
            )
            evidence = ArticleEvidence(
                law_title=request.law_title,
                source_type="law",
                article_no=request.article_no,
                version_status="废止或失效",
                data_source=trace,
            )
            return LookupResult(trace.status, evidence, trace)

    class ExplodingChecker:
        def compare_application(self, *args, **kwargs):
            raise AssertionError("确定性结论已存在，不应调用 LLM")

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            _simple_claim(
                "cl_00001", "依据《合同法》第五十二条。", "合同法", "第五十二条"
            ),
        ],
    )
    frontend_doc = verify_claim_document(
        claim_doc,
        db_path,
        sources=[RepealedSource()],
        semantic_checker=ExplodingChecker(),
    )
    check = frontend_doc.statute_results[0]
    assert check.findings
    assert check.findings[0].code == StatuteErrorCode.SOURCE_REPEALED
    assert check.application_check is None


def test_extraction_respects_scope_selection():
    """未勾选的核查范围在提取阶段即跳过。"""
    from ccitecheck.recognition.service import extract_claims
    from tests.test_rule_engine import _make_parsed_doc

    doc = _make_parsed_doc(
        [
            "依据《民法典》第五百七十七条，应当承担违约责任。",
            "参见（2020）京01民终1号判决。",
        ]
    )
    only_statutes = extract_claims(doc, include_statutes=True, include_cases=False)
    assert all(
        c.claim_type == ClaimType.LEGAL_SOURCE_CLAIM for c in only_statutes.claims
    )
    assert len(only_statutes.claims) == 1

    only_cases = extract_claims(doc, include_statutes=False, include_cases=True)
    assert all(c.claim_type != ClaimType.LEGAL_SOURCE_CLAIM for c in only_cases.claims)
    assert len(only_cases.claims) == 1


def test_locator_revision_replaces_only_wrong_paragraph_number():
    from types import SimpleNamespace
    from ccitecheck.orchestration.scheduler import _CheckItem, _locator_revision
    from ccitecheck.domain.citation import ArticleRef
    from ccitecheck.domain.statute_results import StatuteLocator

    text = "依据《中华人民共和国民法典》第五百零九条第九款，当事人应当按照约定全面履行自己的义务。"
    item = _CheckItem(
        claim=SimpleNamespace(text=text, context_text=text),
        law_title="中华人民共和国民法典", display_title="中华人民共和国民法典",
        article=ArticleRef(article="第五百零九条", paragraphs=["第九款"]),
        article_no="第五百零九条", not_verifiable=None,
    )
    revision = _locator_revision(item, StatuteLocator(
        article_no="第五百零九条", paragraph_no="第一款"
    ))
    assert revision is not None and revision.machine_applicable
    assert revision.revised_text == (
        "依据《中华人民共和国民法典》第五百零九条第一款，"
        "当事人应当按照约定全面履行自己的义务。"
    )


def test_locator_revision_replaces_only_wrong_item_number():
    from types import SimpleNamespace
    from ccitecheck.orchestration.scheduler import _CheckItem, _locator_revision
    from ccitecheck.domain.citation import ArticleRef
    from ccitecheck.domain.statute_results import StatuteLocator

    text = "依据《个人信息保护法》第十三条第一款第九项，处理个人信息应当取得个人同意。"
    item = _CheckItem(
        claim=SimpleNamespace(text=text, context_text=text),
        law_title="个人信息保护法", display_title="个人信息保护法",
        article=ArticleRef(article="第十三条", paragraphs=["第一款"], items=["第九项"]),
        article_no="第十三条", not_verifiable=None,
    )
    revision = _locator_revision(item, StatuteLocator(
        article_no="第十三条", paragraph_no="第一款", item_no="第一项"
    ))
    assert revision is not None and revision.machine_applicable
    assert revision.revised_text == (
        "依据《个人信息保护法》第十三条第一款第一项，处理个人信息应当取得个人同意。"
    )


def test_verified_law_name_repair_has_title_only_revision():
    from types import SimpleNamespace
    from ccitecheck.orchestration.scheduler import _CheckItem, _verified_repair_finding
    from ccitecheck.domain.citation import ArticleRef
    from ccitecheck.domain.statute_results import (
        StatuteLocationCandidate, StatuteLocationResolution, StatuteLocator,
    )

    wrong = "中华人民共和国著作产权实施条例"
    correct = "中华人民共和国著作权法实施条例"
    text = f"根据《{wrong}》，复制、改编等权利具有各自的控制范围。"
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.ARTICLE_FOUND,
    )
    item = _CheckItem(
        claim=SimpleNamespace(text=text, context_text=text),
        law_title=wrong, display_title=wrong,
        article=ArticleRef(article="第一条"), article_no="第一条",
        not_verifiable=None,
        correction_evidence=ArticleEvidence(
            law_title=correct, article_no="第一条", article_text="条文。",
            data_source=trace,
        ),
        repair_verified=True,
        location_resolution=StatuteLocationResolution(
            status="resolved",
            candidates=[StatuteLocationCandidate(
                locator=StatuteLocator(article_no="第一条"),
                text="条文。", supported=True,
            )],
        ),
    )

    finding = _verified_repair_finding(item)
    assert finding is not None and finding.code == StatuteErrorCode.LAW_NAME_ERROR
    assert finding.revision is not None and finding.revision.machine_applicable
    assert finding.revision.revised_text == text.replace(f"《{wrong}》", f"《{correct}》")


def test_de_particle_difference_requires_semantic_evidence():
    from ccitecheck.domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
    from ccitecheck.verification.statutes.locator import resolve_location_candidates

    trace = SourceTrace(tier=SourceTier.LOCAL_SQLITE, source_name="test", status=LookupStatus.ARTICLE_FOUND)
    evidence = ArticleEvidence(
        law_title="个人信息保护法", source_type="law", article_no="第十三条",
        article_text="符合下列情形之一的，个人信息处理者方可处理个人信息：\n（一）取得个人的同意；\n（二）为履行合同所必需。",
        data_source=trace,
    )
    result = resolve_location_candidates(
        "个人信息处理者在取得个人同意后可以处理个人信息。", [evidence]
    )
    assert result.status != "resolved"


def test_bare_multi_law_listing_passes_existence_check_without_article_text(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    text = "本文结合《网络数据安全管理条例》《互联网信息服务算法推荐管理规定》等现行有效法规进行分析。"
    sources = [
        LegalSource(title=title,)
        for title in ("网络数据安全管理条例", "互联网信息服务算法推荐管理规定")
    ]
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(source_doc_id="doc-test", source_doc_hash="sha256:test"),
        claims=[Claim(
            claim_id="cl_00001", claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text=text, anchor_ids=["line00001"],
            entities=LegalSourceClaimEntities(legal_sources=sources),
        )],
    )

    class ExistingLawSource:
        def lookup(self, request):
            trace = SourceTrace(
                tier=SourceTier.PKULAW_FALLBACK, source_name="北大法宝 MCP",
                source_url="https://pkulaw.com/chl/example.html",
                status=LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE,
                message="法规存在",
            )
            evidence = ArticleEvidence(
                law_title=request.law_title, source_type="law",
                version_status="现行有效", data_source=trace,
            )
            return LookupResult(trace.status, evidence, trace)

    result = verify_claim_document(
        claim_doc, db_path, sources=[ExistingLawSource()], include_cases=False
    )
    assert [item.outcome for item in result.statute_results] == ["pass", "pass"]
    assert all(item.evidence.article_text is None for item in result.statute_results)


def test_duplicate_statute_issues_merge_into_single_card_with_all_locations():
    from ccitecheck.orchestration.scheduler import _aggregate_duplicate_statute_results
    from ccitecheck.domain.citation import SourceLocation
    from ccitecheck.domain.evidence import LookupStatus
    from ccitecheck.domain.statute_results import (
        StatuteFinding, StatuteLocator, StatuteVerificationResult,
    )

    def make(check_id, block_id):
        return StatuteVerificationResult(
            check_id=check_id, card_id=f"card_{check_id}", display_group_id="dg_test",
            claim_id=f"cl_{check_id}",
            claim_text="依据《劳动合同法》第八十二条，应支付三倍工资。",
            law_title="中华人民共和国劳动合同法",
            lookup_status=LookupStatus.ARTICLE_FOUND,
            cited_locators=[StatuteLocator(article_no="第八十二条")],
            findings=[StatuteFinding(
                code=StatuteErrorCode.ARTICLE_NUMBER_ERROR, risk_level="HIGH",
                summary="条号错误", suggestion="条号引用错误，应为《劳动合同法》第八十三条",
            )],
            outcome="issue",
            source_locations=[SourceLocation(block_id=block_id, char_start=0, char_end=10)],
        )

    results = [make("a", "blk-1"), make("b", "blk-2"), make("c", "blk-3")]
    merged = _aggregate_duplicate_statute_results(results)

    assert len(merged) == 1
    assert [loc.block_id for loc in merged[0].source_locations] == ["blk-1", "blk-2", "blk-3"]
    assert "共出现 3 处" in merged[0].message
