import base64
import importlib
from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from ccitecheck.infrastructure.database import connect, init_db, upsert_article, upsert_law


def _reject_named_temporary_file(*args, **kwargs):
    raise AssertionError("API must use a closed, reopenable temporary DOCX path")


def test_health_publishes_json_contract_versions():
    api_module = importlib.import_module("apps.api.app")
    payload = TestClient(api_module.app).get("/api/health").json()
    assert payload["claim_schema_version"] == "0.6"
    assert payload["verification_schema_version"] == "0.9"


def test_health_counts_configured_deepseek(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_PLUS_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_MAX_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    api_module = importlib.import_module("apps.api.app")
    payload = TestClient(api_module.app).get("/api/health").json()

    assert payload["llm_configured"] is True

def test_document_check_streams_keepalive_for_slow_processing(tmp_path, monkeypatch):
    """核查耗时超过保活间隔时，响应正文以保活空白开头且仍能被 JSON 解析，
    避免 Word 插件所在 WKWebView 因长时间无数据而超时。"""
    import time
    from io import BytesIO
    import base64, importlib
    from docx import Document

    db_path = tmp_path / "laws.sqlite"
    _seed_law_db(db_path)
    document = Document()
    document.add_paragraph("依据《中华人民共和国民法典》第五百七十七条，被告应当承担违约责任。")
    buffer = BytesIO()
    document.save(buffer)

    api_module = importlib.import_module("apps.api.app")
    monkeypatch.setattr(api_module, "LAW_DB", db_path)
    monkeypatch.setattr(api_module, "KEEPALIVE_SECONDS", 0.05)
    original = api_module._run_document_check

    def slow(*args, **kwargs):
        time.sleep(0.25)  # 跨过多个保活间隔
        return original(*args, **kwargs)

    monkeypatch.setattr(api_module, "_run_document_check", slow)
    client = TestClient(api_module.app)
    response = client.post(
        "/api/checks",
        json={
            "file_name": "test.docx",
            "docx_base64": base64.b64encode(buffer.getvalue()).decode(),
            "semantic_check": False,
        },
    )

    assert response.status_code == 200
    assert response.content[:1] == b" "  # 保活空白开头
    payload = response.json()  # 前导空白不影响 JSON 解析
    assert payload["summary"]["reference_total"] == 1
    assert "__stream_error__" not in payload



def test_word_addin_document_check_api(tmp_path, monkeypatch):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as connection:
        law_id = upsert_law(
            connection,
            {"title": "中华人民共和国民法典", "source_type": "law"},
        )
        upsert_article(
            connection,
            law_id,
            {
                "article_no": "第五百七十七条",
                "text": "当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担违约责任。",
            },
        )

    document = Document()
    document.add_paragraph("依据《中华人民共和国民法典》第五百七十七条，被告应当承担违约责任。")
    buffer = BytesIO()
    document.save(buffer)

    api_module = importlib.import_module("apps.api.app")
    monkeypatch.setattr(api_module, "LAW_DB", db_path)
    monkeypatch.setattr(api_module.tempfile, "NamedTemporaryFile", _reject_named_temporary_file)
    client = TestClient(api_module.app)
    response = client.post(
        "/api/checks",
        json={
            "file_name": "test.docx",
            "docx_base64": base64.b64encode(buffer.getvalue()).decode(),
            "semantic_check": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert "legal_checks" not in payload["verification"]
    assert payload["summary"]["card_total"] == 1
    assert payload["summary"]["reference_total"] == 1
    assert payload["semantic_check"] is False
    assert payload["summary"]["total"] == 1


def test_root_redirects_to_word_taskpane_with_security_headers():
    api_module = importlib.import_module("apps.api.app")
    client = TestClient(api_module.app)

    response = client.get("/", follow_redirects=False)
    taskpane_response = client.get("/taskpane.html")

    assert response.status_code == 307
    assert response.headers["location"] == "/taskpane.html"
    assert response.headers["cache-control"] == "no-cache"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert (
        "script-src 'self' https://appsforoffice.microsoft.com"
        in taskpane_response.headers["content-security-policy"]
    )


def _seed_law_db(db_path):
    init_db(db_path)
    with connect(db_path) as connection:
        law_id = upsert_law(
            connection,
            {"title": "中华人民共和国民法典", "source_type": "law"},
        )
        upsert_article(
            connection,
            law_id,
            {
                "article_no": "第五百七十七条",
                "text": "当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担违约责任。",
            },
        )


def test_selection_check_api(tmp_path, monkeypatch):
    db_path = tmp_path / "laws.sqlite"
    _seed_law_db(db_path)

    api_module = importlib.import_module("apps.api.app")
    monkeypatch.setattr(api_module, "LAW_DB", db_path)
    monkeypatch.setattr(api_module.tempfile, "NamedTemporaryFile", _reject_named_temporary_file)
    client = TestClient(api_module.app)
    response = client.post(
        "/api/checks/selection",
        json={
            "file_name": "test.docx",
            "text": "依据《中华人民共和国民法典》第五百七十七条，被告应当承担违约责任。",
            "source_blocks": [{"block_id": "word:p:7", "char_start": 12}],
            "semantic_check": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["file_name"] == "test.docx（选中片段）"
    assert payload["summary"]["total"] == 1
    assert payload["verification"]["statute_results"][0]["lookup_status"] == "article_found"
    location = payload["verification"]["statute_results"][0]["source_locations"][0]
    assert location["block_id"] == "word:p:7"
    assert location["char_start"] == 12


def test_selection_check_rejects_empty_text(tmp_path, monkeypatch):
    api_module = importlib.import_module("apps.api.app")
    client = TestClient(api_module.app)
    response = client.post(
        "/api/checks/selection",
        json={"file_name": "test.docx", "text": "   \n  ", "semantic_check": False},
    )
    assert response.status_code == 400


def test_case_only_selection_reports_unconfigured_case_source(tmp_path, monkeypatch):
    api_module = importlib.import_module("apps.api.app")
    verification_module = importlib.import_module("ccitecheck.orchestration.scheduler")
    from ccitecheck.retrieval.sources.pkulaw.client import PkulawNotConfiguredError

    class UnconfiguredCaseSource:
        def search_keyword(self, title, fulltext):
            raise PkulawNotConfiguredError("案例数据源未配置")

        def search_semantic(self, text):
            raise PkulawNotConfiguredError("案例数据源未配置")

    monkeypatch.setattr(verification_module, "PkulawCaseSource", UnconfiguredCaseSource)
    monkeypatch.setattr(api_module, "LAW_DB", tmp_path / "laws.sqlite")
    client = TestClient(api_module.app)
    response = client.post(
        "/api/checks/selection",
        json={
            "file_name": "案例研究.docx",
            "text": "指导案例262号具有参考意义。",
            "semantic_check": True,
            "include_statutes": False,
            "include_cases": True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["bugs"] == 1
    assert payload["verification"]["statute_results"] == []
    assert payload["verification"]["case_results"][0]["lookup_status"] == "source_not_configured"
    assert payload["document_key"].startswith("sha256:")


def test_scope_validation_requires_at_least_one(tmp_path, monkeypatch):
    api_module = importlib.import_module("apps.api.app")
    client = TestClient(api_module.app)
    response = client.post(
        "/api/checks/selection",
        json={
            "file_name": "t.docx",
            "text": "依据《中华人民共和国民法典》第五百七十七条。",
            "semantic_check": False,
            "include_statutes": False,
            "include_cases": False,
        },
    )
    assert response.status_code == 400


def test_statutes_can_be_excluded(tmp_path, monkeypatch):
    db_path = tmp_path / "laws.sqlite"
    _seed_law_db(db_path)
    api_module = importlib.import_module("apps.api.app")
    monkeypatch.setattr(api_module, "LAW_DB", db_path)
    client = TestClient(api_module.app)
    response = client.post(
        "/api/checks/selection",
        json={
            "file_name": "t.docx",
            "text": "依据《中华人民共和国民法典》第五百七十七条，应当承担违约责任。",
            "semantic_check": False,
            "include_statutes": False,
            "include_cases": False or True,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["verification"]["statute_results"] == []
