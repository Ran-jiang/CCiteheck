from pathlib import Path

from ccitecheck.infrastructure.database import (
    find_law,
    find_current_article,
    init_db,
    list_article_versions,
    list_historical_article_versions,
    normalize_title,
    seed_common_laws,
    connect,
    upsert_article,
    upsert_law,
)


def test_normalize_title_unifies_book_title_marks_and_parentheses():
    canonical = "最高人民法院关于适用中华人民共和国民法典婚姻家庭编的解释（一）"
    variants = (
        "最高人民法院关于适用《中华人民共和国民法典》婚姻家庭编的解释（一）",
        "最高人民法院关于适用＜中华人民共和国民法典＞婚姻家庭编的解释(一)",
        "最高人民法院关于适用<中华人民共和国民法典>婚姻家庭编的解释(一)",
        "最高人民法院关于适用﹤中华人民共和国民法典﹥婚姻家庭编的解释(一)",
        "最高人民法院 关于适用〈中华人民共和国民法典〉 婚姻家庭编的解释(一)",
    )

    assert {normalize_title(title) for title in variants} == {canonical}


def test_seed_common_laws_and_alias_lookup(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    count = seed_common_laws(db_path)

    assert count >= 40

    with connect(db_path) as conn:
        law = conn.execute(
            "SELECT * FROM laws WHERE normalized_title = ?",
            ("中华人民共和国劳动合同法",),
        ).fetchone()
        assert law is not None

        alias = conn.execute(
            "SELECT * FROM law_aliases WHERE alias = ?",
            ("劳动合同法",),
        ).fetchone()
        assert alias is not None
        assert alias["law_id"] == law["id"]

        expected_aliases = {
            "合同编通则解释": "最高人民法院关于适用《中华人民共和国民法典》合同编通则若干问题的解释",
            "担保制度解释": "最高人民法院关于适用《中华人民共和国民法典》有关担保制度的解释",
            "公司法解释四": "最高人民法院关于适用《中华人民共和国公司法》若干问题的规定（四）",
        }
        for short_title, canonical_title in expected_aliases.items():
            matched = find_law(conn, short_title)
            assert matched is not None
            assert matched["title"] == canonical_title


def test_find_law_toggles_national_prefix_without_stored_alias(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        upsert_law(conn, {"title": "星河数据治理法", "source_type": "law"})
        matched = find_law(conn, "中华人民共和国星河数据治理法")

    assert matched is not None
    assert matched["title"] == "星河数据治理法"


def test_import_bundle_and_find_current_article(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn,
            {
                "title": "中华人民共和国劳动合同法",
                "source_type": "law",
                "authority": "全国人大常委会",
                "category": "劳动用工",
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

    with connect(db_path) as conn:
        article = find_current_article(conn, "劳动合同法", "第三十七条")
        assert article is not None
        assert article["title"] == "中华人民共和国劳动合同法"
        assert "提前三十日" in article["text"]


def test_find_current_article_selects_version_by_effective_date(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(
            conn,
            {
                "title": "中华人民共和国商标法",
                "source_type": "law",
                "status": "has_articles",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第一条",
                "text": "现行版本。",
                "version_key": "2019-04-23",
                "version_label": "现行有效",
                "version_status": "effective",
                "effective_from": "2019-04-23",
                "effective_to": "2027-01-01",
            },
        )
        upsert_article(
            conn,
            law_id,
            {
                "article_no": "第一条",
                "text": "未来版本。",
                "version_key": "2027-01-01",
                "version_label": "尚未生效",
                "version_status": "future_effective",
                "effective_from": "2027-01-01",
            },
        )

    with connect(db_path) as conn:
        current = find_current_article(conn, "商标法", "第一条", as_of="2026-07-10")
        future = find_current_article(conn, "商标法", "第一条", as_of="2027-01-01")

    assert current["text"] == "现行版本。"
    assert current["version_key"] == "2019-04-23"
    assert future["text"] == "未来版本。"
    assert future["version_status"] == "future_effective"


def test_list_article_versions_separates_historical_from_current(tmp_path: Path):
    db_path = tmp_path / "laws.sqlite"
    init_db(db_path)
    with connect(db_path) as conn:
        law_id = upsert_law(conn, {
            "title": "中华人民共和国示例法",
            "source_type": "law",
            "status": "has_articles",
        })
        upsert_article(conn, law_id, {
            "article_no": "第二条",
            "text": "历史版本第二条。",
            "version_key": "2018",
            "effective_from": "2018-01-01",
            "effective_to": "2020-01-01",
        })
        upsert_article(conn, law_id, {
            "article_no": "第二条",
            "text": "现行版本第二条。",
            "version_key": "2020",
            "effective_from": "2020-01-01",
        })

        versions = list_article_versions(conn, "示例法", "第二条")
        historical = list_historical_article_versions(
            conn, "示例法", "第二条", as_of="2026-01-01"
        )

    assert [row["version_key"] for row in versions] == ["2020", "2018"]
    assert [row["version_key"] for row in historical] == ["2018"]
