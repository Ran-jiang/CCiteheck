from pathlib import Path

from ccitecheck.infrastructure.database import connect, init_db, upsert_article, upsert_law
from ccitecheck.infrastructure.runtime_checks import check_runtime


def test_check_runtime_reports_ready_law_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(conn, {"title": "中华人民共和国测试法", "source_type": "law"})
        upsert_article(conn, law_id, {"article_no": "第一条", "text": "测试。"})

    monkeypatch.setenv("PKULAW_ACCESS_TOKEN", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "")
    monkeypatch.setenv("ANSVAR_MCP_GATEWAY", "")

    results = check_runtime(db_path)

    assert results[0].ok
    assert "1 laws" in results[0].message
    # 语义核查已改为默认必配：缺少 DASHSCOPE_API_KEY 时 doctor 报 FAIL
    assert not results[1].ok
    assert "DASHSCOPE_API_KEY" in results[1].message
    assert results[2].ok
    assert results[2].message == "optional fallback not configured"
    assert results[4].message == "optional multi-jurisdiction source not configured"


def test_check_runtime_reports_missing_db(tmp_path: Path):
    results = check_runtime(tmp_path / "missing.sqlite")

    assert not results[0].ok


def test_check_runtime_accepts_current_pkulaw_token(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn, {"title": "中华人民共和国测试法", "source_type": "law"}
        )
        upsert_article(conn, law_id, {"article_no": "第一条", "text": "测试。"})
    monkeypatch.setenv("PKULAW_ACCESS_TOKEN", "token")

    results = check_runtime(db_path)

    assert results[2].ok
    assert results[2].message == "statute and case MCP sources configured"
