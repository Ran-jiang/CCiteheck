"""EUR-Lex 溯源源与 EU 法域链路的单元测试（不依赖网络）。"""

from pathlib import Path

import ccitecheck.orchestration.scheduler as verify_claims_module
from ccitecheck.application.verify_claims import verify_claim_document
from ccitecheck.domain.citation import (
    ArticleRef,
    Claim,
    ClaimDocument,
    ClaimMeta,
    ClaimType,
    LegalSource,
    LegalSourceClaimEntities,
)
from ccitecheck.domain.evidence import LookupStatus, SourceTier
from ccitecheck.infrastructure.database import init_db
from ccitecheck.retrieval.sources.base import LookupRequest
from ccitecheck.retrieval.sources.eurlex.client import (
    EurLexMcpError,
    EurLexRecord,
    _parse_search_response,
)
from ccitecheck.retrieval.sources.eurlex.statutes import EurLexSource


GDPR_RECORD = EurLexRecord(
    title="Regulation (EU) 2016/679 (General Data Protection Regulation)",
    celex="32016R0679",
    url="https://eur-lex.europa.eu/eli/reg/2016/679/oj",
    in_force=True,
)


class FakeEurLexClient:
    def __init__(self, records=None, error=None):
        self.records = records or []
        self.error = error
        self.queries = []

    def search_law(self, query, celex=""):
        self.queries.append((query, celex))
        if self.error is not None:
            raise self.error
        return self.records


def _request(title="通用数据保护条例", article_no=None):
    return LookupRequest(
        law_title=title, article_no=article_no
    )


# ---------- 响应解析 ----------

def test_parse_search_response_accepts_plain_results_list():
    payload = {"result": {"results": [
        {"title": "Regulation (EU) 2016/679", "celex": "32016R0679",
         "url": "https://eur-lex.europa.eu/eli/reg/2016/679/oj", "in_force": True},
    ]}}
    records = _parse_search_response(payload)
    assert len(records) == 1
    assert records[0].celex == "32016R0679"
    assert records[0].in_force is True


def test_parse_search_response_accepts_mcp_text_content():
    payload = {"result": {"content": [{"type": "text", "text":
        '{"documents": [{"name": "Artificial Intelligence Act", "uri": "https://eur-lex.europa.eu/x"}]}'
    }]}}
    records = _parse_search_response(payload)
    assert records and records[0].title == "Artificial Intelligence Act"
    assert records[0].url == "https://eur-lex.europa.eu/x"


def test_parse_search_response_builds_official_link_from_celex():
    records = _parse_search_response({"results": [
        {"title": "Artificial Intelligence Act", "celex": "32024R1689"},
    ]})
    assert records[0].url == (
        "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32024R1689"
    )


# ---------- 源适配器 ----------

def test_eurlex_source_confirms_existence_with_alias_query():
    client = FakeEurLexClient(records=[GDPR_RECORD])
    result = EurLexSource(client=client).lookup(_request())
    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert result.trace.tier == SourceTier.EURLEX
    assert result.evidence is not None
    assert result.evidence.version_status == "现行有效"
    assert result.evidence.source_metadata["celex"] == "32016R0679"
    # 别名表把中文名翻译成官方英文检索词
    assert "General Data Protection Regulation" in client.queries[0][0]
    assert client.queries[0][1] == "32016R0679"


def test_eurlex_source_not_found_and_error_paths():
    not_found = EurLexSource(client=FakeEurLexClient(records=[])).lookup(_request())
    assert not_found.status == LookupStatus.LAW_NOT_FOUND
    assert not_found.evidence is None
    # 未找到不应触发"法源不存在"式误报：metadata 不带 search_completed
    assert "search_completed" not in not_found.trace.metadata

    error = EurLexSource(
        client=FakeEurLexClient(error=EurLexMcpError("HTTP 502"))
    ).lookup(_request())
    assert error.status == LookupStatus.SOURCE_ERROR


def test_ai_act_uses_live_source_instead_of_embedded_excerpt():
    client = FakeEurLexClient(records=[])
    result = EurLexSource(client=client).lookup(
        _request(title="人工智能法", article_no="第五十条")
    )
    assert result.status == LookupStatus.LAW_NOT_FOUND
    assert client.queries == [("Artificial Intelligence Act 2024/1689", "32024R1689")]


def test_eurlex_source_not_configured_without_gateway(monkeypatch):
    # 置空而非删除：本机 .env 配置了真实网关，空串可挡住 load_project_env 的 setdefault
    monkeypatch.setenv("EURLEX_MCP_GATEWAY", "")
    result = EurLexSource().lookup(_request())
    assert result.status == LookupStatus.SOURCE_NOT_CONFIGURED


# ---------- EU 法域端到端链路 ----------

def test_eu_statute_routes_to_eurlex_and_skips_semantic(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("EURLEX_MCP_GATEWAY", "https://eurlex.test")
    monkeypatch.setattr(
        verify_claims_module,
        "sources_for_jurisdiction",
        lambda jurisdiction, cn_chain=None: [
            EurLexSource(client=FakeEurLexClient(records=[GDPR_RECORD]))
        ] if jurisdiction == "EU" else cn_chain,
    )
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
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
                text="欧盟《通用数据保护条例》正式生效。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title="通用数据保护条例",

                            jurisdiction="EU",
                            articles=[],
                        )
                    ]
                ),
            )
        ],
    )
    frontend_doc = verify_claim_document(claim_doc, db_path, include_cases=False)
    check = frontend_doc.statute_results[0]
    assert check.jurisdiction == "EU"
    assert check.lookup_status == LookupStatus.RELEVANT_ARTICLES_FOUND
    # 无条号引用只核验存在性，不做语义核查
    assert not hasattr(check, "meaning_check")
    assert check.outcome == "pass"
    assert check.evidence.source_metadata["celex"] == "32016R0679"


# ---------- 条号引用：取回 Article 原文并进入语义比对 ----------

GDPR_ARTICLE_17 = (
    "Article 17\n\nRight to erasure (‘right to be forgotten’)\n\n"
    "1\\. The data subject shall have the right to obtain from the controller "
    "the erasure of personal data concerning him or her without undue delay…"
)


class FakeEurLexClientWithDocument(FakeEurLexClient):
    def __init__(self, records=None, article=None, article_error=None):
        super().__init__(records=records)
        self.article = article
        self.article_error = article_error
        self.article_calls = []

    def get_article_text(self, celex, article_number):
        self.article_calls.append((celex, article_number))
        if self.article_error is not None:
            raise self.article_error
        return self.article


def test_eurlex_source_fetches_cited_article_text():
    client = FakeEurLexClientWithDocument(
        records=[GDPR_RECORD],
        article={"text": GDPR_ARTICLE_17, "title": "Regulation (EU) 2016/679", "in_force": True},
    )
    result = EurLexSource(client=client).lookup(_request(article_no="第十七条"))
    assert result.status == LookupStatus.ARTICLE_FOUND
    assert "Right to erasure" in result.evidence.article_text
    assert client.article_calls == [("32016R0679", 17)]


def test_eurlex_article_fetch_failure_degrades_to_existence():
    client = FakeEurLexClientWithDocument(
        records=[GDPR_RECORD], article_error=EurLexMcpError("HTTP 502")
    )
    result = EurLexSource(client=client).lookup(_request(article_no="第十七条"))
    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert result.evidence is not None


def test_eu_article_citation_goes_through_application_check(tmp_path: Path, monkeypatch):
    from ccitecheck.domain.statute_results import LegalApplicationCheck

    class PassChecker:
        def __init__(self):
            self.calls = []

        def compare_application(self, original_text, authorities):
            self.calls.append({
                "claim_text": original_text,
                "statute_text": authorities[0].article_text,
            })
            return LegalApplicationCheck(verdict="pass")

    monkeypatch.setenv("EURLEX_MCP_GATEWAY", "https://eurlex.test")
    client = FakeEurLexClientWithDocument(
        records=[GDPR_RECORD],
        article={"text": GDPR_ARTICLE_17, "title": "Regulation (EU) 2016/679", "in_force": True},
    )
    monkeypatch.setattr(
        verify_claims_module,
        "sources_for_jurisdiction",
        lambda jurisdiction, cn_chain=None: [EurLexSource(client=client)]
        if jurisdiction == "EU" else cn_chain,
    )
    checker = PassChecker()
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test", source_doc_hash="sha256:test", source_file="t.docx"
        ),
        claims=[Claim(
            claim_id="cl_00001",
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text="根据欧盟《通用数据保护条例》第十七条第九款，数据主体享有被遗忘权。",
            anchor_ids=["line00001"],
            entities=LegalSourceClaimEntities(legal_sources=[LegalSource(
                title="通用数据保护条例",

                jurisdiction="EU",
                articles=[ArticleRef(article="第十七条", paragraphs=["第九款"])],
            )]),
        )],
    )
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    frontend_doc = verify_claim_document(
        claim_doc, db_path, semantic_checker=checker, include_cases=False
    )
    check = frontend_doc.statute_results[0]
    assert check.lookup_status == LookupStatus.ARTICLE_FOUND
    assert not check.findings
    assert check.application_check.verdict == "pass"
    assert not hasattr(check, "meaning_check")
    assert checker.calls and "Right to erasure" in checker.calls[0]["statute_text"]
    assert checker.calls[0]["claim_text"] == claim_doc.claims[0].text


def test_eu_unfaithful_quote_is_reported_by_application_check(tmp_path: Path, monkeypatch):
    from ccitecheck.domain.statute_results import (
        LegalApplicationCheck, LegalApplicationReview,
    )

    class IssueChecker:
        def compare_application(self, original_text, authorities):
            return LegalApplicationCheck(
                verdict="review",
                reviews=[LegalApplicationReview(
                    error_type="meaning_distorted",
                    summary="文书写数据可携权，原文是第十七条的删除权，可携权实际规定在第二十条",
                    suggestion="引文不忠实于权威原文，请按原文修正。",
                )],
            )

    monkeypatch.setenv("EURLEX_MCP_GATEWAY", "https://eurlex.test")
    client = FakeEurLexClientWithDocument(
        records=[GDPR_RECORD],
        article={"text": GDPR_ARTICLE_17, "title": "Regulation (EU) 2016/679", "in_force": True},
    )
    monkeypatch.setattr(
        verify_claims_module,
        "sources_for_jurisdiction",
        lambda jurisdiction, cn_chain=None: [EurLexSource(client=client)]
        if jurisdiction == "EU" else cn_chain,
    )
    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test", source_doc_hash="sha256:test", source_file="t.docx"
        ),
        claims=[Claim(
            claim_id="cl_00001",
            claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
            text="根据欧盟《通用数据保护条例》第十七条，数据主体享有数据可携权。",
            anchor_ids=["line00001"],
            entities=LegalSourceClaimEntities(legal_sources=[LegalSource(
                title="通用数据保护条例",

                jurisdiction="EU",
                articles=[ArticleRef(article="第十七条")],
            )]),
        )],
    )
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    frontend_doc = verify_claim_document(
        claim_doc, db_path, semantic_checker=IssueChecker(), include_cases=False
    )
    check = frontend_doc.statute_results[0]
    assert check.application_check.reviews[0].error_type == "meaning_distorted"
    assert check.outcome == "review"
    assert check.evidence.article_no == "Article 17"
