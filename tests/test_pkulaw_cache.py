"""北大法宝缓存层与并发去重的测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from ccitecheck.domain.evidence import LookupStatus
from ccitecheck.retrieval.sources.base import LookupRequest
from ccitecheck.retrieval.sources.pkulaw.cache import (
    CachedPkulawClient,
    TTL_SECONDS,
    cache_clear,
    cache_status,
    connect_cache,
)
from ccitecheck.retrieval.sources.pkulaw.client import (
    PkulawArticle,
    PkulawLawRecord,
    PkulawNotFoundError,
    PkulawMcpError,
)
from ccitecheck.retrieval.sources.pkulaw.models import PkulawRecognizedLaw
from ccitecheck.retrieval.sources.pkulaw.statutes import PkulawFallbackSource


class FakeClient:
    """记录调用次数的假 MCP 客户端。"""

    def __init__(self, timeliness=("现行有效",)):
        self.article_calls = 0
        self.list_calls = 0
        self.timeliness = list(timeliness)
        self.semantic_calls = 0
        self.recognition_calls = 0
        self.law_item_calls = 0

    def get_article(self, title, article_no):
        self.article_calls += 1
        if "不存在" in title:
            raise PkulawNotFoundError("未找到数据")
        return PkulawArticle(
            title=title,
            url="https://www.pkulaw.com/x",
            timeliness=self.timeliness,
            article_no=article_no,
            article_text="条文内容",
        )

    def search_law_articles(self, text):
        self.semantic_calls += 1
        return [
            PkulawArticle(
                title="中华人民共和国民法典",
                article_no="第一条",
                article_text="缓存条文",
            )
        ]

    def get_law_item_content(self, title, article_no):
        self.law_item_calls += 1
        return PkulawArticle(
            title=title,
            article_no=article_no,
            article_text="条文全文",
            implement_date="2021-01-01",
            timeliness=["现行有效"],
        )

    def get_law_list(self, title="", fulltext=""):
        self.list_calls += 1
        if "不存在" in title:
            raise PkulawNotFoundError("未找到数据")
        return [
            PkulawLawRecord(title=title, url="https://x", timeliness=self.timeliness)
        ]

    def recognize_laws(self, text):
        self.recognition_calls += 1
        return [PkulawRecognizedLaw(
            mentioned_title=text,
            canonical_title=text,
            fulltext="第一条　缓存的法规全文。",
            url="https://pkulaw.com/chl/cached.html",
        )]


@pytest.fixture()
def cache_db(tmp_path):
    return tmp_path / "cache.sqlite"


def test_get_article_success_and_negative_results_are_cached(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)
    first = cached.get_article("中华人民共和国民法典", "第一条")
    second = cached.get_article("中华人民共和国民法典", "第一条")
    assert first.article_text == second.article_text
    assert fake.article_calls == 1

    for _ in range(2):
        with pytest.raises(PkulawNotFoundError):
            cached.get_article("不存在的法", "第一条")
    assert fake.article_calls == 2


def test_article_semantic_results_are_cached(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)
    first = cached.search_law_articles_for_article("中华人民共和国民法典", "第一条")
    second = cached.search_law_articles_for_article("中华人民共和国民法典", "第一条")
    assert fake.semantic_calls == 1
    assert second[0].article_text == first[0].article_text


def test_location_semantic_and_law_item_results_are_cached_once(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)

    with ThreadPoolExecutor(max_workers=4) as pool:
        semantic = list(pool.map(cached.search_law_articles, ["合同解除"] * 4))
        items = list(pool.map(
            lambda _: cached.get_law_item_content("中华人民共和国民法典", "第一条"),
            range(4),
        ))

    assert fake.semantic_calls == 1
    assert fake.law_item_calls == 1
    assert all(result[0].article_text == "缓存条文" for result in semantic)
    assert all(item.article_text == "条文全文" for item in items)


def test_equivalent_article_numbers_share_one_cache_entry(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)

    cached.get_law_item_content("中华人民共和国民法典", "第十四条")
    cached.get_law_item_content("中华人民共和国民法典", "第14条")

    assert fake.law_item_calls == 1


def test_mismatched_exact_article_is_not_cached(cache_db):
    fake = FakeClient()
    fake.get_law_item_content = lambda title, article_no: PkulawArticle(
        title="无关培训通知",
        article_no=article_no,
        article_text="无关内容",
    )
    cached = CachedPkulawClient(fake, cache_db)

    with pytest.raises(PkulawMcpError, match="法名或条号不匹配"):
        cached.get_law_item_content("中华人民共和国民法典", "第一条")

    with connect_cache(cache_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM cache_entries").fetchone()[0] == 0


def test_law_recognition_fulltext_is_cached(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)

    first = cached.recognize_laws("中华人民共和国民法典")
    second = cached.recognize_laws("中华人民共和国民法典")

    assert fake.recognition_calls == 1
    assert second[0].fulltext == first[0].fulltext


def test_empty_article_semantic_result_is_negative_cached(cache_db):
    fake = FakeClient()
    fake.search_law_articles = lambda text: (
        setattr(fake, "semantic_calls", fake.semantic_calls + 1) or []
    )
    cached = CachedPkulawClient(fake, cache_db)
    assert (
        cached.search_law_articles_for_article("中华人民共和国民法典", "第一条") == []
    )
    assert (
        cached.search_law_articles_for_article("中华人民共和国民法典", "第一条") == []
    )
    assert fake.semantic_calls == 1


def test_not_found_article_semantic_result_is_negative_cached(cache_db):
    fake = FakeClient()

    def not_found(text):
        fake.semantic_calls += 1
        raise PkulawNotFoundError("未找到数据")

    fake.search_law_articles = not_found
    cached = CachedPkulawClient(fake, cache_db)
    with pytest.raises(PkulawNotFoundError):
        cached.search_law_articles_for_article("中华人民共和国民法典", "第一条")
    assert (
        cached.search_law_articles_for_article("中华人民共和国民法典", "第一条") == []
    )
    assert fake.semantic_calls == 1


def test_repeating_same_lookup_uses_only_cached_pkulaw_results(cache_db):
    fake = FakeClient()
    source = PkulawFallbackSource(CachedPkulawClient(fake, cache_db))
    request = LookupRequest(
        law_title="中华人民共和国民法典",
        article_no="第五百七十七条",
        context_text="当事人一方不履行合同义务，应当承担违约责任。",
    )
    first = source.lookup(request)
    calls_after_first = (fake.article_calls, fake.law_item_calls, fake.list_calls)
    second = source.lookup(request)
    assert first.status == second.status == LookupStatus.ARTICLE_FOUND
    assert calls_after_first == (0, 1, 1)
    assert (fake.article_calls, fake.law_item_calls, fake.list_calls) == calls_after_first


@pytest.mark.parametrize("status", ["废止或失效", "已被修改", "部分失效"])
def test_non_current_law_list_is_not_cached_as_effective(cache_db, status):
    fake = FakeClient(timeliness=(status,))
    cached = CachedPkulawClient(fake, cache_db)
    cached.get_law_list(title="中华人民共和国合同法")
    cached.get_law_list(title="中华人民共和国合同法")
    assert fake.list_calls == 1
    with connect_cache(cache_db) as conn:
        row = conn.execute("SELECT status, payload FROM cache_entries").fetchone()
    assert row["status"] == "repealed"
    assert "url" in row["payload"]
    assert status in row["payload"]


def test_old_minimal_repealed_law_cache_is_refetched(cache_db):
    fake = FakeClient(timeliness=("废止或失效",))
    with connect_cache(cache_db) as conn:
        conn.execute(
            "INSERT INTO cache_entries VALUES (?, ?, ?, ?, ?, ?)",
            (
                "law",
                "中华人民共和国合同法",
                "repealed",
                '[{"title":"中华人民共和国合同法","timeliness":["废止或失效"]}]',
                1,
                9999999999,
            ),
        )
        conn.commit()

    records = CachedPkulawClient(fake, cache_db).get_law_list(
        title="中华人民共和国合同法"
    )

    assert fake.list_calls == 1
    assert records[0].url == "https://x"


def test_law_cache_uses_matching_record_not_first_candidate(cache_db):
    fake = FakeClient()
    fake.get_law_list = lambda title="", fulltext="": [
        PkulawLawRecord(title="中华人民共和国合同法", timeliness=["废止"]),
        PkulawLawRecord(title="中华人民共和国民法典", timeliness=["现行有效"]),
    ]
    CachedPkulawClient(fake, cache_db).get_law_list(title="民法典")
    with connect_cache(cache_db) as conn:
        row = conn.execute("SELECT status, payload FROM cache_entries").fetchone()
    assert row["status"] == "effective"
    assert "url" in row["payload"]


def test_expired_effective_entry_revalidates_instead_of_refetch(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)
    cached.get_article("中华人民共和国民法典", "第五百七十七条")
    # 人为把 verified_at 拨回过期
    with connect_cache(cache_db) as conn:
        conn.execute(
            "UPDATE cache_entries SET verified_at = verified_at - ?",
            (TTL_SECONDS["effective"] + 10,),
        )
        conn.commit()
    result = cached.get_article("中华人民共和国民法典", "第五百七十七条")
    # 只做了一次轻量时效验证（get_law_list），没有重拉条文全文
    assert fake.article_calls == 1
    assert fake.list_calls == 1
    assert result.article_text == "条文内容"


def test_unexpected_revalidation_error_is_not_swallowed(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)
    cached.get_article("中华人民共和国民法典", "第一条")
    with connect_cache(cache_db) as conn:
        conn.execute(
            "UPDATE cache_entries SET verified_at = verified_at - ?",
            (TTL_SECONDS["effective"] + 10,),
        )
        conn.commit()

    def broken_list(title="", fulltext=""):
        raise TypeError("parser contract broken")

    fake.get_law_list = broken_list
    with pytest.raises(TypeError, match="parser contract broken"):
        cached.get_article("中华人民共和国民法典", "第一条")


def test_known_revalidation_service_error_renews_cached_data_for_one_day(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)
    cached.get_article("中华人民共和国民法典", "第一条")
    with connect_cache(cache_db) as conn:
        conn.execute(
            "UPDATE cache_entries SET verified_at = verified_at - ?",
            (TTL_SECONDS["effective"] + 10,),
        )
        conn.commit()

    fake.get_law_list = lambda title="", fulltext="": (_ for _ in ()).throw(
        PkulawMcpError("network")
    )
    result = cached.get_article("中华人民共和国民法典", "第一条")
    assert result.article_text == "条文内容"
    assert fake.article_calls == 1


def test_cache_status_and_clear(cache_db):
    fake = FakeClient()
    cached = CachedPkulawClient(fake, cache_db)
    cached.get_article("中华人民共和国民法典", "第五百七十七条")
    info = cache_status(cache_db)
    assert info["groups"] and info["groups"][0]["n"] == 1
    assert cache_clear(cache_db) == 1
    assert cache_status(cache_db)["groups"] == []
