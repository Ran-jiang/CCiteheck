"""Ansvar MCP 客户端与多法域法规适配器测试（不访问网络）。"""

from pathlib import Path

import pytest

from ccitecheck.domain.evidence import LookupStatus
from ccitecheck.retrieval.sources.ansvar.auth import AnsvarOAuth
from ccitecheck.retrieval.sources.ansvar.client import (
    AnsvarMcpClient,
    AnsvarMcpError,
    AnsvarQuotaExceededError,
    AnsvarRecord,
    _classify_jsonrpc_error,
    _map_args_to_schema,
    _parse_search_response,
)
from ccitecheck.retrieval.sources.ansvar.cache import CachedAnsvarClient
from ccitecheck.retrieval.sources.ansvar.statutes import AnsvarSource, _pick_match
from ccitecheck.retrieval.sources.base import LookupRequest
from ccitecheck.verification.statutes.deterministic import assess_statute


class FakeAnsvarClient:
    def __init__(self, records=None, article=None, article_error=None):
        self.records = records or []
        self.article = article
        self.article_error = article_error
        self.search_calls = []
        self.article_calls = []

    def search_law(self, query, jurisdiction=""):
        self.search_calls.append((query, jurisdiction))
        return self.records

    def get_article_text(
        self, identifier, article_number, *, jurisdiction="", lookup_arguments=None
    ):
        self.article_calls.append(
            (identifier, article_number, jurisdiction, lookup_arguments)
        )
        if self.article_error:
            raise self.article_error
        if isinstance(self.article, list):
            return self.article.pop(0)
        return self.article


def _request(article_no="第二条", jurisdiction="DE"):
    return LookupRequest(
        law_title="Urheberrechtsgesetz",
        article_no=article_no,
        jurisdiction=jurisdiction,
    )


def test_client_requires_explicit_gateway(monkeypatch):
    monkeypatch.setenv("ANSVAR_MCP_GATEWAY", "")
    with pytest.raises(AnsvarMcpError):
        AnsvarMcpClient()


def test_search_parser_preserves_exact_lookup_hint():
    payload = {"result": {"structuredContent": {"results": [{
        "law_title": "Urheberrechtsgesetz",
        "canonical_ref": "DE:UrhG:2",
        "jurisdiction": "DE",
        "_citation": {
            "source_url": "https://www.gesetze-im-internet.de/urhg/__2.html",
            "publisher": "BMJ",
            "license": "DL-DE-BY-2.0",
            "last_verified": "2026-09-18",
            "resolved_canonical_ref": "DE:UrhG:2",
            "resolution_method": "exact",
            "lookup": {
                "tool": "get_provision",
                "arguments": {"canonical_ref": "DE:UrhG:2"},
            },
        },
    }]}}}
    record = _parse_search_response(payload)[0]
    assert record.identifier == "DE:UrhG:2"
    assert record.lookup_arguments == {"canonical_ref": "DE:UrhG:2"}
    assert record.publisher == "BMJ"
    assert record.citation["resolution_method"] == "exact"


def test_search_parser_accepts_gateway_law_metadata_rows():
    payload = {"result": {"structuredContent": {"results": [{
        "text": "인공지능 발전과 신뢰 기반 조성 등에 관한 기본법 (인공지능기본법)",
        "citation": {
            "jurisdiction": "KR",
            "source": "act-282791",
            "article": "meta",
            "source_url": "https://www.law.go.kr/LSW/lsInfoP.do?lsiSeq=282791",
            "lookup": {"args": {
                "canonical_ref": "act-282791:meta", "jurisdiction": "KR"
            }},
        },
    }]}}}
    record = _parse_search_response(payload)[0]
    assert record.title == "인공지능 발전과 신뢰 기반 조성 등에 관한 기본법"
    assert record.identifier == "act-282791"
    assert record.jurisdiction == "KR"
    assert record.lookup_arguments == {}


def test_article_parser_preserves_citation_contract(monkeypatch):
    client = AnsvarMcpClient(gateway="https://ansvar.test", access_token="token")
    monkeypatch.setattr(
        client, "_resolve_article_tool", lambda: ("get_provision", {})
    )
    monkeypatch.setattr(client, "_call_tool", lambda *_: {
        "result": {"structuredContent": {"results": [{
            "text": "Article 13\n\nRight to information",
            "citation": {
                "source_url": "https://example.test/article-13",
                "publisher": "Official publisher",
                "license": "CC-BY",
                "last_verified": "2026-09-18",
                "resolved_canonical_ref": "EU:GDPR:13",
                "resolution_method": "exact",
            },
        }]}}
    })
    article = client.get_article_text("GDPR", "13", jurisdiction="EU")
    assert article["last_verified"] == "2026-09-18"
    assert article["version_date"] == ""
    assert article["resolved_canonical_ref"] == "EU:GDPR:13"
    assert article["citation"]["resolution_method"] == "exact"


def test_article_parser_turns_tool_not_found_into_exact_miss(monkeypatch):
    client = AnsvarMcpClient(gateway="https://ansvar.test", access_token="token")
    monkeypatch.setattr(
        client, "_resolve_article_tool", lambda: ("get_provision", {})
    )
    monkeypatch.setattr(client, "_call_tool", lambda *_: {
        "result": {
            "isError": True,
            "content": [{"type": "text", "text": "provision not found"}],
        }
    })
    assert client.get_article_text("Missing Act", "999") is None


def test_schema_mapping_preserves_declared_types():
    schema = {"properties": {
        "query": {"type": "string"},
        "jurisdiction": {"type": "string"},
        "limit": {"type": "integer"},
    }}
    assert _map_args_to_schema(
        schema, {"query": "copyright", "jurisdictions": ["DE"], "limit": 5}
    ) == {"query": "copyright", "jurisdiction": "DE", "limit": 5}


def test_only_cap_exceeded_is_classified_as_quota():
    quota = _classify_jsonrpc_error({
        "code": -32000,
        "message": "request refused",
        "data": {"cause": "cap_exceeded"},
    })
    generic = _classify_jsonrpc_error({"code": -32000, "message": "source unavailable"})
    assert isinstance(quota, AnsvarQuotaExceededError)
    assert type(generic) is AnsvarMcpError


def test_source_uses_exact_lookup_before_search():
    client = FakeAnsvarClient(article={
        "text": "§ 2\n\nProtected works include...",
        "title": "Urheberrechtsgesetz",
        "url": "https://example.test/section-2",
        "last_verified": "2026-09-18",
        "resolved_canonical_ref": "DE:UrhG:2",
        "resolution_method": "exact",
        "citation": {"publisher": "BMJ", "resolution_method": "exact"},
    })
    result = AnsvarSource(client).lookup(_request())
    assert result.status == LookupStatus.ARTICLE_FOUND
    assert result.evidence.article_text == "Protected works include..."
    assert result.evidence.source_metadata["last_verified"] == "2026-09-18"
    assert result.evidence.source_metadata["citation"]["resolution_method"] == "exact"
    assert client.search_calls == []
    assert client.article_calls == [("Urheberrechtsgesetz", "2", "DE", None)]


def test_source_falls_back_to_search_after_exact_miss():
    record = AnsvarRecord(
        title="Urheberrechtsgesetz",
        identifier="DE:UrhG",
        jurisdiction="DE",
        url="https://example.test/law",
        lookup_arguments={"canonical_ref": "DE:UrhG:2"},
    )
    client = FakeAnsvarClient(records=[record], article=[None, {
        "text": "§ 2\n\nProtected works include...",
        "url": "https://example.test/section-2",
    }])
    result = AnsvarSource(client).lookup(_request())
    assert result.status == LookupStatus.ARTICLE_FOUND
    assert result.evidence.article_text == "Protected works include..."
    assert client.search_calls == [("Urheberrechtsgesetz", "DE")]
    assert client.article_calls == [
        ("Urheberrechtsgesetz", "2", "DE", None),
        ("DE:UrhG", "2", "DE", {"canonical_ref": "DE:UrhG:2"})
    ]


def test_source_does_not_display_gateway_document_id_as_law_title():
    record = AnsvarRecord(
        title="인공지능 발전과 신뢰 기반 조성 등에 관한 기본법",
        identifier="act-282791",
        jurisdiction="KR",
    )
    client = FakeAnsvarClient(records=[record], article=[None, {
        "text": "제31조 인공지능 투명성 확보 의무",
        "title": "act-282791",
    }])
    result = AnsvarSource(client).lookup(LookupRequest(
        law_title="人工智能发展及建立信任基础基本法",
        article_no="第31条",
        jurisdiction="KR",
    ))
    assert result.evidence.law_title == record.title


def test_cached_client_caches_found_and_not_found(tmp_path: Path):
    found = FakeAnsvarClient(article={"text": "Article 2"})
    cached = CachedAnsvarClient(found, tmp_path / "ansvar.sqlite")
    assert cached.get_article_text("Law", "2", jurisdiction="DE") == {"text": "Article 2"}
    assert cached.get_article_text("Law", "2", jurisdiction="DE") == {"text": "Article 2"}
    assert len(found.article_calls) == 1

    missing = FakeAnsvarClient(article=None)
    cached_missing = CachedAnsvarClient(missing, tmp_path / "missing.sqlite")
    assert cached_missing.get_article_text("Missing", "9") is None
    assert cached_missing.get_article_text("Missing", "9") is None
    assert len(missing.article_calls) == 1


def test_cached_exact_miss_does_not_hide_canonical_lookup_hint(tmp_path: Path):
    client = FakeAnsvarClient(article=[None, {"text": "Article 2"}])
    cached = CachedAnsvarClient(client, tmp_path / "ansvar.sqlite")
    assert cached.get_article_text("Law", "2") is None
    assert cached.get_article_text(
        "Law", "2", lookup_arguments={"canonical_ref": "DE:Law:2"}
    ) == {"text": "Article 2"}
    assert len(client.article_calls) == 2


def test_take_it_down_uses_live_source_instead_of_embedded_excerpt():
    client = FakeAnsvarClient()
    result = AnsvarSource(client).lookup(LookupRequest(
        law_title="TAKE IT DOWN Act", article_no="第3条", jurisdiction="US"
    ))
    assert result.status == LookupStatus.LAW_NOT_FOUND
    assert client.search_calls == [("TAKE IT DOWN Act", "US")]


def test_korean_ai_basic_act_uses_official_title_and_jurisdiction():
    client = FakeAnsvarClient(article={"text": "제31조\n\n인공지능 투명성 확보 의무"})
    result = AnsvarSource(client).lookup(LookupRequest(
        law_title="人工智能发展及建立信任基础基本法",
        article_no="第31条",
    ))
    assert result.status == LookupStatus.ARTICLE_FOUND
    assert client.article_calls == [(
        "인공지능 발전과 신뢰 기반 조성 등에 관한 기본법",
        "31",
        "KR",
        None,
    )]


def test_article_fetch_error_is_not_reported_as_existence_success():
    client = FakeAnsvarClient(
        records=[AnsvarRecord(
            title="Urheberrechtsgesetz", identifier="DE:UrhG", jurisdiction="DE"
        )],
        article_error=AnsvarMcpError("source unavailable"),
    )
    result = AnsvarSource(client).lookup(_request())
    assert result.status == LookupStatus.SOURCE_ERROR
    assert result.evidence is None


def test_explicit_jurisdiction_never_accepts_other_country():
    records = [AnsvarRecord(title="Copyright Act", jurisdiction="GB")]
    assert _pick_match(records, "DE") is None


def test_completed_ansvar_miss_becomes_source_not_found_finding():
    result = AnsvarSource(FakeAnsvarClient()).lookup(_request(article_no=None))
    findings = assess_statute(
        "Urheberrechtsgesetz", None, result, [result.trace], []
    )
    assert findings[0].code.value == "source_not_found"
    assert "Ansvar" in findings[0].summary


def test_refresh_keeps_unrotated_refresh_token(tmp_path: Path, monkeypatch):
    oauth = AnsvarOAuth(token_cache=tmp_path / "tokens.json")
    oauth._client_id = "client"
    oauth._save_tokens({
        "access_token": "old",
        "refresh_token": "refresh",
        "expires_at": 0,
    })
    monkeypatch.setattr(
        oauth,
        "_refresh",
        lambda token: {"access_token": "new", "refresh_token": None, "expires_at": 99999999999},
    )
    assert oauth.get_bearer() == "new"
    assert oauth._load_tokens()["refresh_token"] == "refresh"
