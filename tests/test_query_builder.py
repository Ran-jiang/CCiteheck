from ccitecheck.query_construction.strategies.common import (
    build_case_keyword_query,
    build_case_semantic_query,
    build_law_fulltext_query,
    build_law_semantic_query,
    build_law_title_query,
)


def test_law_queries_strip_version_and_separate_fulltext_terms():
    context = (
        "依据《网络安全法（2025修正）》第十二条，网络运营者应当保护用户信息，"
        "并采取必要的安全措施。"
    )

    assert build_law_title_query("《网络安全法（2025修正）》") == "中华人民共和国网络安全法"
    assert build_law_fulltext_query(context, "网络安全法") == (
        "网络运营者应当保护用户信息 并采取必要的安全措施"
    )
    semantic = build_law_semantic_query(context, "网络安全法（2025修正）")
    assert semantic.startswith("在《中华人民共和国网络安全法》中检索")
    assert "第十二条" not in semantic


def test_law_title_matching_accepts_ordinal_revision_annotation():
    from ccitecheck.query_construction.matching import equivalent_law_titles

    title = "最高人民法院关于审理民间借贷案件适用法律若干问题的规定"
    assert equivalent_law_titles(title, title + "(2020第二次修正)")


def test_case_queries_keep_case_name_and_remove_empty_connectors():
    context = "最高人民法院在指导案例262号中认为，平台应当采取必要措施。"

    title, fulltext = build_case_keyword_query(
        "指导案例262号", context, "最高人民法院"
    )
    semantic = build_case_semantic_query(
        "指导案例262号", context, "最高人民法院"
    )

    assert title == "指导案例262号"
    assert fulltext == "平台应当采取必要措施"
    assert "案例线索：指导案例262号" in semantic
    assert "法院：最高人民法院" in semantic
