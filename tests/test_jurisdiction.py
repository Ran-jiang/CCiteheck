"""法域识别与超边界拦截（out_of_scope）的单元测试。"""

from pathlib import Path

from ccitecheck.application.verify_claims import verify_claim_document
from ccitecheck.domain.citation import (
    ArticleRef,
    Claim,
    ClaimDocument,
    ClaimMeta,
    ClaimType,
    CaseCitationEntities,
    CaseRef,
    LegalSource,
    LegalSourceClaimEntities,
)
from ccitecheck.domain.evidence import CaseLookupStatus, LookupStatus
from ccitecheck.infrastructure.database import init_db
from ccitecheck.orchestration.cases import verify_case_claims
from ccitecheck.recognition.cases import extract_case_refs
from ccitecheck.query_construction.jurisdiction import (
    detect_jurisdiction,
    detect_jurisdiction_with_basis,
)
from ccitecheck.query_construction.source_planner import plan_sources
from ccitecheck.recognition.statutes import extract_legal_sources


# ---------- 法域检测 ----------

def test_detect_jurisdiction_by_adjacent_prefix():
    assert detect_jurisdiction("著作权法", "大陆法系国家多规定，德国") == "DE"
    assert detect_jurisdiction("著作权法", "……参见法国") == "FR"
    assert detect_jurisdiction("人工智能统一规则", "2021年4月，欧盟") == "EU"
    assert detect_jurisdiction("著作权法", "依据我国") == "CN"
    assert detect_jurisdiction("著作权法", "") == "CN"


def test_detect_jurisdiction_generic_mentions_do_not_trigger():
    # 泛称不紧邻书名号时不应触发
    assert detect_jurisdiction("著作权法", "大陆法系国家多有规定，我国") == "CN"


def test_detect_jurisdiction_by_alias_table():
    assert detect_jurisdiction("通用数据保护条例", "") == "EU"
    assert detect_jurisdiction("知识产权法典", "") == "FR"
    assert detect_jurisdiction("GDPR", "") == "EU"


def test_korean_ai_basic_act_routes_to_ansvar():
    jurisdiction = detect_jurisdiction("人工智能发展及建立信任基础基本法", "")
    assert jurisdiction == "KR"
    assert [item.source_id for item in plan_sources(jurisdiction)] == ["ansvar"]


def test_detect_jurisdiction_inside_title_and_reject_conflicts():
    assert detect_jurisdiction("德国著作权法", "") == "DE"
    assert detect_jurisdiction("欧盟人工智能统一规则", "") == "EU"
    assert detect_jurisdiction("中国数据安全法", "") == "CN"
    assert detect_jurisdiction_with_basis(
        "德国著作权法", "法国"
    ) == ("UNKNOWN", "conflicting_explicit_signals")


def test_recognition_does_not_carry_jurisdiction():
    sources = extract_legal_sources(
        "德国《著作权法》第2条与法国《知识产权法典》均有规定；"
        "我国《著作权法》第三条另有定义。"
    )
    assert all(source.jurisdiction is None for source in sources)

    assert detect_jurisdiction("著作权法", "德国") == "DE"
    assert detect_jurisdiction("著作权法", "依据我国") == "CN"
    assert detect_jurisdiction("通用数据保护条例", "欧盟") == "EU"


# ---------- 外国判例识别 ----------

def test_extract_case_refs_flags_foreign_citations():
    refs = extract_case_refs(
        "美国联邦最高法院在 Roe v. Wade 案及 347 U.S. 483 判例中确立了相关规则。"
    )
    foreign = [r for r in refs if r.jurisdiction == "UNKNOWN"]
    assert any("Roe" in (r.case_name or "") for r in foreign)
    assert any("U.S." in (r.case_name or "") for r in foreign)


def test_extract_case_refs_chinese_cases_stay_cn():
    refs = extract_case_refs("在腾讯公司诉上海盈讯公司著作权侵权纠纷案中，法院认为……")
    assert refs and all(r.jurisdiction == "CN" for r in refs)


# ---------- 法规侧拦截 ----------

def _statute_claim_doc(jurisdiction: str, title: str = "著作权法") -> ClaimDocument:
    return ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
                text=f"德国《{title}》第2条另有规定。",
                anchor_ids=["line00001"],
                entities=LegalSourceClaimEntities(
                    legal_sources=[
                        LegalSource(
                            title=title,

                            jurisdiction=jurisdiction,
                            articles=[ArticleRef(article="第二条")],
                        )
                    ]
                ),
            )
        ],
    )


def test_foreign_statute_is_intercepted_without_lookup(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    frontend_doc = verify_claim_document(
        _statute_claim_doc("FOREIGN"), db_path, include_cases=False
    )
    check = frontend_doc.statute_results[0]
    assert check.lookup_status == LookupStatus.OUT_OF_SCOPE
    assert check.evidence is None
    assert not check.findings
    assert check.outcome == "bug"
    assert check.source_attempts[0].source_name == "CCiteCheck 法域分类"
    assert "当前未覆盖自动核查" in check.source_attempts[0].message


def test_eu_statute_without_gateway_reports_not_configured(
    tmp_path: Path, monkeypatch
):
    # 置空而非删除：本机 .env 配置了真实网关，空串可挡住 load_project_env 的 setdefault
    monkeypatch.setenv("EURLEX_MCP_GATEWAY", "")
    monkeypatch.setenv("ANSVAR_MCP_GATEWAY", "")
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    frontend_doc = verify_claim_document(
        _statute_claim_doc("EU", "通用数据保护条例"), db_path, include_cases=False
    )
    check = frontend_doc.statute_results[0]
    assert check.lookup_status == LookupStatus.SOURCE_NOT_CONFIGURED
    assert "欧盟法规数据源未配置" in check.source_attempts[0].message


# ---------- 案例侧拦截 ----------

def test_foreign_case_is_intercepted_without_recognizer_calls():
    class ExplodingRecognizer:
        def __getattr__(self, name):
            raise AssertionError("外国判例不应触发任何案例数据源调用")

    claim_doc = ClaimDocument(
        claim_meta=ClaimMeta(
            source_doc_id="doc-test",
            source_doc_hash="sha256:test",
            source_file="test.docx",
        ),
        claims=[
            Claim(
                claim_id="cl_00001",
                claim_type=ClaimType.CASE_CITATION,
                text="美国联邦最高法院在 Roe v. Wade 案中确立了相关规则。",
                anchor_ids=["line00001"],
                entities=CaseCitationEntities(
                    case_refs=[
                        CaseRef(

                            case_name="Roe v. Wade",
                            jurisdiction="FOREIGN",
                        )
                    ]
                ),
            )
        ],
    )
    checks = verify_case_claims(claim_doc, ExplodingRecognizer())
    assert len(checks) == 1
    assert checks[0].lookup_status == CaseLookupStatus.OUT_OF_SCOPE
    assert "不支持的外国法域" in checks[0].message
