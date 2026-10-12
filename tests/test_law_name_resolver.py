from ccitecheck.query_construction.providers.pkulaw import resolve_law_name
from ccitecheck.retrieval.sources.pkulaw.client import PkulawArticle, PkulawLawRecord


class _Client:
    def __init__(self, articles):
        self.articles = articles

    def search_law_articles(self, text):
        return self.articles

    def get_law_list(self, title="", fulltext=""):
        return [PkulawLawRecord(title=title, timeliness=["现行有效"])]


def test_resolver_accepts_unique_exact_article_and_safe_title_suffix():
    client = _Client([PkulawArticle(
        title="中华人民共和国城市房地产管理法",
        article_no="第三十八条",
        article_text="房地产转让规则。",
        url="https://example.test/article",
    )])

    resolved = resolve_law_name(
        client,
        raw_left_window="依照城市房地产管理法",
        article_no="第38条",
        context_text="依照城市房地产管理法第38条处理。",
    )

    assert resolved is not None
    assert resolved.surface_title == "城市房地产管理法"
    assert resolved.canonical_title == "中华人民共和国城市房地产管理法"


def test_resolver_rejects_wrong_article_number():
    client = _Client([PkulawArticle(
        title="中华人民共和国城市房地产管理法",
        article_no="第三十九条",
        article_text="无关条文。",
    )])

    assert resolve_law_name(
        client,
        raw_left_window="依照城市房地产管理法",
        article_no="第38条",
        context_text="依照城市房地产管理法第38条处理。",
    ) is None


def test_resolver_rejects_multiple_matching_laws():
    client = _Client([
        PkulawArticle(title="中华人民共和国测试法", article_no="第十条", article_text="A"),
        PkulawArticle(title="测试法", article_no="第十条", article_text="B"),
    ])

    assert resolve_law_name(
        client,
        raw_left_window="依据测试法",
        article_no="第10条",
        context_text="依据测试法第10条。",
    ) is None
