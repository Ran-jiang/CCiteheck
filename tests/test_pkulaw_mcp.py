import json

import pytest

from ccitecheck.domain.evidence import LookupStatus
from ccitecheck.domain.statute_results import StatuteErrorCode
from ccitecheck.retrieval.sources.base import LookupRequest
from ccitecheck.retrieval.sources.pkulaw.client import (
    MCP_ENDPOINTS,
    PkulawArticle,
    PkulawLawRecord,
    PkulawMcpClient,
    PkulawMcpError,
    PkulawNotFoundError,
    normalize_article_no,
)
from ccitecheck.retrieval.sources.pkulaw.corpus import split_recognized_fulltext
from ccitecheck.retrieval.sources.pkulaw.models import PkulawRecognizedLaw
from ccitecheck.retrieval.sources.pkulaw.statutes import PkulawFallbackSource
from ccitecheck.verification.statutes import assess_statute


class FakePkulawClient(PkulawMcpClient):
    def __init__(self, payload):
        super().__init__(access_token="test-token")
        self.payload = payload

    def _call_tool(self, endpoint, tool_name, arguments):
        return self.payload


class SimilarTitleRecallClient:
    def __init__(self, hit):
        self.hit = hit
        self.calls = []

    def get_law_list(self, title="", fulltext=""):
        self.calls.append(title)
        if title == self.hit:
            return [PkulawLawRecord(title="中华人民共和国领海及毗连区法")]
        raise PkulawNotFoundError("未找到数据")


def _mcp_text_payload(data):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(data, ensure_ascii=False),
                }
            ]
        },
    }


@pytest.mark.parametrize("hit_index", [0, 2])
def test_similar_title_recall_uses_prefixes_then_suffix_and_stops(hit_index):
    typo = "错误海域管理毗连区法"
    queries = [typo[:len(typo) * 2 // 3], typo[:len(typo) // 2], typo[-4:]]
    client = SimilarTitleRecallClient(queries[hit_index])

    titles = PkulawFallbackSource(client)._recall_similar_titles(typo)

    assert client.calls == queries[:hit_index + 1]
    assert titles == ["中华人民共和国领海及毗连区法"]


def test_get_article_uses_exact_semantic_tool_and_normalizes_number():
    client = CapturingPkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": {
                    "title": "中华人民共和国民法典",
                    "article": "第四十八条　条文内容",
                    "url": "https://x",
                },
            }
        )
    )
    article = client.get_article("民法典", "第48条")
    assert article.article_no == "第四十八条"
    assert article.article_text == "条文内容"
    assert client.calls == [
        (
            MCP_ENDPOINTS["law_semantic"],
            "get_article",
            {"title": "民法典", "number": "第四十八条"},
        )
    ]


def test_get_article_empty_text_is_a_clean_miss():
    client = FakePkulawClient(_mcp_text_payload({
        "title": "中华人民共和国刑法(2023修正)",
        "article": "",
        "timeliness": "现行有效",
    }))

    with pytest.raises(PkulawNotFoundError):
        client.get_article("刑法", "第453条")


def test_get_article_keeps_real_gateway_version_metadata():
    client = FakePkulawClient(_mcp_text_payload({
        "title": "中华人民共和国刑法(2023修正)",
        "article": "第四百五十二条　本法自1997年10月1日起施行。",
        "doc_no": "主席令",
        "timeliness": "现行有效",
        "implementation_date": "2024-03-01",
    }))

    article = client.get_article("刑法", "第452条")

    assert article.document_no == "主席令"
    assert article.timeliness == ["现行有效"]
    assert article.implement_date == "2024-03-01"


def test_law_item_uses_fatiao_and_parses_fulltext_metadata():
    client = CapturingPkulawClient(_mcp_text_payload({
        "Title": "中华人民共和国刑法(2023修正)",
        "FullText": "第四百五十二条　本法自1997年10月1日起施行。",
        "ImplementDate": "2024.03.01",
        "TimelinessDic": ["现行有效"],
        "Url": "https://pkulaw.com/chl/example.html#tiao_452.0",
    }))

    article = client.get_law_item_content("刑法", "第四百五十二条")

    assert article.article_text.startswith("本法自")
    assert article.implement_date == "2024.03.01"
    assert article.url.endswith("#tiao_452.0")
    assert client.calls == [(
        MCP_ENDPOINTS["law_item"],
        "get_law_item_content",
        {"title": "刑法", "tiao_num": 452},
    )]


def test_exact_article_rejects_silently_wrong_returned_number():
    client = FakePkulawClient(_mcp_text_payload({
        "Title": "中华人民共和国刑法(2023修正)",
        "FullText": "第一百二十条　错误返回了本条而非之一。",
    }))

    with pytest.raises(PkulawMcpError, match="第一百二十条"):
        client.get_article("刑法", "第一百二十条之一")


def test_law_item_sends_article_suffix_as_number():
    client = CapturingPkulawClient(_mcp_text_payload({
        "Title": "中华人民共和国刑法(2023修正)",
        "FullText": "第一百二十条之一　条文。",
    }))

    client.get_law_item_content("刑法", "第一百二十条之一")

    assert client.calls[0][2]["tiao_num"] == 120.1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("第48条", "第四十八条"),
        ("第184条之1", "第一百八十四条之一"),
        ("第一百八十四条之一", "第一百八十四条之一"),
        ("非法输入", "非法输入"),
    ],
)
def test_normalize_article_no(raw, expected):
    assert normalize_article_no(raw) == expected


def test_get_law_list_parses_candidates():
    client = FakePkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": [
                    {
                        "Title": "中华人民共和国民法典",
                        "Url": "[北大法宝](https://example.com)",
                        "IssueDepartment": ["全国人民代表大会"],
                        "TimelinessDic": ["现行有效"],
                    }
                ],
            }
        )
    )

    records = client.get_law_list(title="民法典")

    assert len(records) == 1
    assert records[0].title == "中华人民共和国民法典"
    assert records[0].issue_department == ["全国人民代表大会"]


def test_get_law_list_discards_obsolete_lar_mcp_url():
    client = FakePkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": [{
                    "Title": "中华人民共和国商标法",
                    "Url": "[北大法宝](https://www.pkulaw.com/lar/dead.html?way=mcp)",
                }],
            }
        )
    )

    assert client.get_law_list(title="商标法")[0].url is None


def test_law_recognition_uses_official_endpoint_and_parses_fulltext():
    client = CapturingPkulawClient(_mcp_text_payload([
        {
            "text": "民法典",
            "original": "中华人民共和国民法典",
            "fulltext": "第一条　为了保护民事主体的合法权益，制定本法。",
            "source": "https://pkulaw.com/chl/example.html",
        }
    ]))

    laws = client.recognize_laws("民法典")

    assert laws[0].canonical_title == "中华人民共和国民法典"
    assert laws[0].fulltext.startswith("第一条")
    assert client.calls == [(
        MCP_ENDPOINTS["law_recognition"],
        "law_recognition",
        {"text": "民法典"},
    )]


def test_recognition_fulltext_splits_articles_without_mixing_boundaries():
    corpus = split_recognized_fulltext(
        "第一条　第一条正文。\r\n\r\n第二条　第二条第一款。\r\n第二条第二款。"
    )

    assert corpus.structurally_valid is True
    assert corpus.structure == "article"
    assert [(item.article_no, item.text) for item in corpus.chunks] == [
        ("第一条", "第一条正文。"),
        ("第二条", "第二条第一款。\n第二条第二款。"),
    ]


def test_recognition_fulltext_without_articles_keeps_paragraph_locators():
    corpus = split_recognized_fulltext(
        "一、总体要求\n\n坚持依法治理，健全工作机制。\n\n加强组织保障。"
    )

    assert corpus.structure == "paragraph"
    assert [item.article_no for item in corpus.chunks] == ["", ""]
    assert [item.locator for item in corpus.chunks] == [
        "一、总体要求 · 段落1",
        "一、总体要求 · 段落2",
    ]


class CapturingPkulawClient(FakePkulawClient):
    def __init__(self, payload):
        super().__init__(payload)
        self.calls = []

    def _call_tool(self, endpoint, tool_name, arguments):
        self.calls.append((endpoint, tool_name, arguments))
        return self.payload


@pytest.mark.parametrize(
    ("method", "args", "endpoint", "tool_name", "arguments"),
    [
        (
            "get_law_list",
            ("民法典", "违约责任"),
            MCP_ENDPOINTS["law_keyword"],
            "get_law_list",
            {"title": "民法典", "fulltext": "违约责任"},
        ),
        (
            "get_case_list",
            ("指导案例262号", "平台 责任"),
            MCP_ENDPOINTS["case_keyword"],
            "get_case_list",
            {"title": "指导案例262号", "fulltext": "平台 责任"},
        ),
    ],
)
def test_keyword_tools_use_official_mcp_paths_and_arguments(
    method, args, endpoint, tool_name, arguments
):
    payload = _mcp_text_payload({"Message": "成功", "Data": []})
    client = CapturingPkulawClient(payload)

    getattr(client, method)(*args)

    assert client.calls == [(endpoint, tool_name, arguments)]


def test_semantic_tools_parse_law_and_case_records():
    law_client = CapturingPkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": [
                    {
                        "Title": "中华人民共和国民法典",
                        "FullText": "第五百七十七条　当事人一方不履行合同义务，应当承担违约责任。",
                        "Url": "https://example.com/law/577",
                    }
                ],
            }
        )
    )
    case_client = CapturingPkulawClient(
        _mcp_text_payload(
            {
                "Message": "成功",
                "Data": [
                    {
                        "Title": "指导性案例262号：某平台纠纷案",
                        "CaseNO": "（2024）最高法民终262号",
                        "Court": "最高人民法院",
                        "Url": "https://example.com/case/262",
                    }
                ],
            }
        )
    )

    articles = law_client.search_law_articles("违约责任")
    cases = case_client.search_cases("指导案例262号")

    assert articles[0].article_no == "第五百七十七条"
    assert articles[0].article_text.startswith("当事人")
    assert law_client.calls[0][:2] == (
        MCP_ENDPOINTS["law_semantic"],
        "search_article",
    )
    assert cases[0].case_number == "（2024）最高法民终262号"
    assert case_client.calls[0][:2] == (
        MCP_ENDPOINTS["case_semantic"],
        "search_case",
    )


def test_search_article_parses_gateway_lowercase_article_field():
    """search_article 实际返回的条文正文字段名是 ``article``（全小写）。

    此前解析器只认 FullText/ArticleText，命中的记录会被逐条跳过，接口不报错
    却恒返回空列表——语义查条因此长期静默失效。
    """
    client = CapturingPkulawClient(
        _mcp_text_payload(
            [
                {
                    "gid": "aa00daaeb5a4fe4ebdfb",
                    "title": "中华人民共和国民法典",
                    "article": "第五百零四条　法人的法定代表人超越权限订立的合同…",
                    "doc_no": "中华人民共和国主席令第45号",
                    "timeliness": "现行有效",
                    "url": "https://pkulaw.com/chl/aa00daaeb5a4fe4ebdfb.html",
                }
            ]
        )
    )

    articles = client.search_law_articles("法定代表人越权订立合同的效力")

    assert len(articles) == 1
    assert articles[0].title == "中华人民共和国民法典"
    assert articles[0].article_no == "第五百零四条"
    # 条号前缀按既定设计由 strip_article_heading 剥离，正文从条文本体开始。
    assert articles[0].article_text.startswith("法人的法定代表人")


def test_current_access_token_and_gateway_configuration(monkeypatch):
    monkeypatch.setenv("PKULAW_ACCESS_TOKEN", "current-token")
    monkeypatch.setenv("PKULAW_MCP_GATEWAY", "https://apim-gateway.pkulaw.com")

    client = PkulawMcpClient()

    assert client.access_token == "current-token"
    assert client.gateway == "https://apim-gateway.pkulaw.com"


class RoutingClient:
    def __init__(self, *, exact=None, semantic=None, laws=None):
        self.exact = exact
        self.semantic = [] if semantic is None else semantic
        self.laws = [] if laws is None else laws
        self.calls = []

    @staticmethod
    def _resolve(value):
        if isinstance(value, Exception):
            raise value
        return value

    def get_article(self, title, article_no):
        self.calls.append(("get_article", title, article_no))
        value = self._resolve(self.exact)
        if value is None:
            raise PkulawNotFoundError("未找到数据")
        return value

    def search_law_articles_for_article(self, title, article_no):
        self.calls.append(("semantic_exact", title, article_no))
        return self._resolve(self.semantic)

    def search_law_articles(self, text):
        self.calls.append(("semantic", text))
        return self._resolve(self.semantic)

    def get_law_list(self, title="", fulltext=""):
        self.calls.append(("law_list", title))
        return self._resolve(self.laws)


class RecognitionCorpusClient(RoutingClient):
    def __init__(self, recognized, *, laws):
        super().__init__(laws=laws)
        self.recognized = recognized

    def recognize_laws(self, text):
        self.calls.append(("law_recognition", text))
        return self.recognized


class CanonicalRecognitionClient(RecognitionCorpusClient):
    def get_article(self, title, article_no):
        self.calls.append(("get_article", title, article_no))
        if title == self.recognized[0].canonical_title:
            return PkulawArticle(
                title=title,
                article_no=article_no,
                article_text="违反本条例的行为，依照规定处罚。",
            )
        raise PkulawNotFoundError("未找到数据")


class ShortTitleOnlyClient(RoutingClient):
    def get_article(self, title, article_no):
        self.calls.append(("get_article", title, article_no))
        if title == "治安管理处罚条例":
            return PkulawArticle(
                title="中华人民共和国治安管理处罚条例",
                article_no=article_no,
                article_text="违反治安管理的行为，依照规定处罚。",
            )
        raise PkulawNotFoundError("未找到数据")


class LawItemRoutingClient(RoutingClient):
    def __init__(self, law_item, **kwargs):
        super().__init__(**kwargs)
        self.law_item = law_item

    def get_law_item_content(self, title, article_no):
        self.calls.append(("law_item", title, article_no))
        return self._resolve(self.law_item)


LAW = PkulawLawRecord(
    title="中华人民共和国民法典",
    timeliness=["现行有效"],
    effectiveness=["法律"],
    implement_date="2021-01-01",
)


def _article(
    article_no="第四十八条", text="保护当事人的合法民事权益。", title=LAW.title
):
    return PkulawArticle(title=title, article_no=article_no, article_text=text)


def _request(article_no="第四十八条", context="保护当事人的合法民事权益。"):
    return LookupRequest(
        law_title="民法典",
        article_no=article_no,
        context_text=context,
    )


def test_numbered_exact_hit_enriches_timeliness_and_records_route_order():
    client = RoutingClient(exact=_article(), laws=[LAW])
    result = PkulawFallbackSource(client).lookup(_request())
    assert result.status == LookupStatus.ARTICLE_FOUND
    assert result.evidence.version_status == "现行有效"
    assert [
        (x["service"], x["status"]) for x in result.trace.metadata["route_attempts"]
    ] == [("law_search_get_article", "completed"), ("law_keyword", "completed")]
    assert (
        result.trace.metadata["route_attempts"][1]["purpose"] == "timeliness_enrichment"
    )


def test_numbered_bare_title_uses_returned_article_version_for_timeliness():
    current = PkulawArticle(
        title="中华人民共和国刑法(2023修正)",
        article_no="第十七条",
        article_text="已满十六周岁的人犯罪，应当负刑事责任。",
    )
    client = RoutingClient(exact=current, laws=[
        PkulawLawRecord(
            title="中华人民共和国刑法",
            timeliness=["已被修改"],
        ),
        PkulawLawRecord(
            title=current.title,
            timeliness=["现行有效"],
        ),
    ])

    result = PkulawFallbackSource(client).lookup(LookupRequest(
        law_title="刑法",
        article_no="第十七条",
    ))

    assert result.evidence.version_status == "现行有效"
    assert ("law_list", current.title) in client.calls


def test_numbered_explicit_revision_queries_that_version_and_reports_latest():
    historical = PkulawArticle(
        title="中华人民共和国民事诉讼法（2017修正）",
        article_no="第四十条",
        article_text="人民法院审理第一审民事案件，由审判员组成合议庭。",
    )
    laws = [
        PkulawLawRecord(
            title=historical.title,
            timeliness=["已被修改"],
            implement_date="2017.07.01",
        ),
        PkulawLawRecord(
            title="中华人民共和国民事诉讼法（2023修正）",
            timeliness=["现行有效"],
            implement_date="2024.01.01",
        ),
    ]
    current = historical.__class__(
        title="中华人民共和国民事诉讼法（2023修正）",
        article_no=historical.article_no,
        article_text="人民法院审理第一审民事案件，由审判员组成合议庭。",
        timeliness=["现行有效"],
    )
    client = LawItemRoutingClient(current, exact=historical, laws=laws)

    result = PkulawFallbackSource(client).lookup(LookupRequest(
        law_title="中华人民共和国民事诉讼法",
        article_no="第四十条",
        version_hint="2017年修正",
    ))
    finding = assess_statute(
        "中华人民共和国民事诉讼法", "第四十条", result, [result.trace], []
    )[0]

    assert client.calls[0] == (
        "law_item", "中华人民共和国民事诉讼法（2017修正）", "第四十条"
    )
    assert next(call for call in client.calls if call[0] == "get_article") == (
        "get_article", "中华人民共和国民事诉讼法（2017修正）", "第四十条"
    )
    assert finding.code == StatuteErrorCode.SOURCE_AMENDED
    assert "2023修正" in finding.suggestion


def test_location_candidate_query_returns_same_law_articles():
    client = RoutingClient(semantic=[
        _article("第二条", "第一款。\n第二款正确内容。"),
        _article("第三条", "其他内容。", title="其他法"),
    ])

    result = PkulawFallbackSource(client).locate_candidates(
        _request("第二条", "第二款正确内容。")
    )

    assert [candidate.article_no for candidate in result.candidates] == ["第二条"]
    assert result.trace.metadata["route_attempts"][0]["purpose"] == "citation_location"


def test_cross_law_location_candidate_gets_timeliness():
    obsolete_law = PkulawLawRecord(
        title="中华人民共和国合同法",
        timeliness=["废止或失效"],
        effectiveness=["法律"],
    )
    obsolete_article = PkulawArticle(
        title=obsolete_law.title,
        article_no="第六十条",
        article_text="当事人应当按照约定全面履行自己的义务。",
        effectiveness=["法律"],
    )
    result = PkulawFallbackSource(RoutingClient(
        semantic=[obsolete_article], laws=[obsolete_law]
    )).locate_candidates(LookupRequest(
        law_title="刑法",
        context_text="当事人应当按照约定全面履行自己的义务。",
    ))

    assert result.candidates[0].version_status == "废止或失效"
    assert result.candidates[0].source_metadata["timeliness"] == ["废止或失效"]


def test_location_candidates_are_enriched_by_law_item():
    item = PkulawArticle(
        title=LAW.title,
        article_no="第二条",
        article_text="第二款正确内容。",
        implement_date="2021-01-01",
        timeliness=["现行有效"],
        url="https://example.com#tiao_2.0",
    )
    client = LawItemRoutingClient(
        item,
        semantic=[_article("第二条", "第二款正确内容。")],
    )

    result = PkulawFallbackSource(client).locate_candidates(
        _request("第三条", "第二款正确内容。")
    )

    assert result.candidates[0].source_metadata["version_key"] == "2021-01-01"
    assert result.candidates[0].source_metadata["version_confirmed"] is True
    assert result.candidates[0].data_source.source_url.endswith("#tiao_2.0")


def test_location_candidates_limit_law_item_enrichment_to_three():
    articles = [
        _article(f"第{number}条", f"合同解除条件第{number}种。")
        for number in range(1, 6)
    ]
    client = LawItemRoutingClient(
        PkulawArticle(
            title=LAW.title,
            article_no="第一条",
            article_text="合同解除条件第一种。",
            implement_date="2021-01-01",
            timeliness=["现行有效"],
        ),
        semantic=articles,
    )

    result = PkulawFallbackSource(client).locate_candidates(
        _request("第九条", "合同解除条件。")
    )

    assert len(result.candidates) == 3
    assert len([call for call in client.calls if call[0] == "law_item"]) == 3


def test_nearby_scan_stops_after_two_articles_each_side():
    class NearbyClient(RoutingClient):
        def get_article(self, title, article_no):
            self.calls.append(("get_article", title, article_no))
            text = "正确的离婚条件。" if article_no == "第47条" else "无关内容。"
            return _article(article_no, text)

        def get_law_item_content(self, title, article_no):
            self.calls.append(("law_item", title, article_no))
            text = "正确的离婚条件。" if article_no == "第47条" else "无关内容。"
            return PkulawArticle(
                title=LAW.title,
                article_no=article_no,
                article_text=text,
                implement_date="2021-01-01",
                timeliness=["现行有效"],
            )

    client = NearbyClient(semantic=[])
    result = PkulawFallbackSource(client).locate_candidates(
        _request("第48条", "正确的离婚条件。")
    )

    assert [candidate.article_no for candidate in result.candidates] == ["第47条"]
    assert len([call for call in client.calls if call[0] == "law_item"]) == 5
    assert not any(call[0] == "get_article" for call in client.calls)
    attempt = result.trace.metadata["route_attempts"][1]
    assert attempt["expanded"] is False
    assert attempt["requested_count"] == 5


def test_complete_cited_article_can_disable_nearby_scan():
    client = LawItemRoutingClient(
        PkulawArticle(
            title=LAW.title,
            article_no="第十四条",
            article_text="已经取得的完整条文。",
            implement_date="2021-01-01",
            timeliness=["现行有效"],
        ),
        semantic=[],
    )

    result = PkulawFallbackSource(client).locate_candidates(LookupRequest(
        law_title=LAW.title,
        article_no="第十四条",
        context_text="概括表述。",
        skip_nearby_scan=True,
    ))

    assert result.candidates == []
    assert not any(call[0] == "law_item" for call in client.calls)


def test_nearby_scan_expands_when_narrow_range_has_no_candidate():
    class NearbyClient(RoutingClient):
        def get_article(self, title, article_no):
            self.calls.append(("get_article", title, article_no))
            text = "正确的离婚条件。" if article_no == "第40条" else "无关内容。"
            return _article(article_no, text)

    client = NearbyClient(semantic=[])
    result = PkulawFallbackSource(client).locate_candidates(
        _request("第48条", "正确的离婚条件。")
    )

    assert [candidate.article_no for candidate in result.candidates] == ["第40条"]
    assert len([call for call in client.calls if call[0] == "get_article"]) == 21
    attempt = result.trace.metadata["route_attempts"][1]
    assert attempt["expanded"] is True
    assert attempt["requested_count"] == 21


def test_location_search_scans_nearby_when_semantic_only_repeats_cited_article():
    class NearbyClient(RoutingClient):
        def get_article(self, title, article_no):
            self.calls.append(("get_article", title, article_no))
            text = "现行利率不得超过贷款市场报价利率四倍。" if article_no == "第25条" else "其他内容。"
            return _article(article_no, text)

    client = NearbyClient(semantic=[_article("第26条", "旧版利率不得超过贷款市场报价利率四倍。")])
    result = PkulawFallbackSource(client).locate_candidates(
        _request("第26条", "现行利率不得超过贷款市场报价利率四倍。")
    )

    assert any(candidate.article_no == "第25条" for candidate in result.candidates)
    assert any(
        attempt["service"] == "law_exact_nearby_scan"
        for attempt in result.trace.metadata["route_attempts"]
    )


def test_numbered_mismatched_exact_result_is_ignored_and_uses_semantic():
    client = RoutingClient(
        exact=_article(title="中华人民共和国刑法"), semantic=[_article()], laws=[LAW]
    )
    result = PkulawFallbackSource(client).lookup(_request())
    assert result.status == LookupStatus.ARTICLE_FOUND
    assert result.trace.metadata["route_attempts"][0]["status"] == "mismatched"
    assert any(call[0] == "semantic_exact" for call in client.calls)


def test_numbered_exact_miss_semantic_same_number_is_article_found():
    client = RoutingClient(semantic=[_article()], laws=[LAW])
    result = PkulawFallbackSource(client).lookup(_request())
    assert result.status == LookupStatus.ARTICLE_FOUND


def test_numbered_short_title_uses_recognized_canonical_title_after_exact_miss():
    canonical = "中华人民共和国治安管理处罚条例"
    client = CanonicalRecognitionClient(
        [PkulawRecognizedLaw(
            mentioned_title="治安管理处罚条例",
            canonical_title=canonical,
            fulltext="",
        )],
        laws=[PkulawLawRecord(title=canonical, timeliness=["失效"])],
    )
    result = PkulawFallbackSource(client).lookup(LookupRequest(
        law_title="治安管理处罚条例",
        article_no="第五十一条",
    ))

    assert result.status == LookupStatus.ARTICLE_FOUND
    assert result.evidence.law_title == canonical
    assert result.evidence.version_status == "失效"
    assert ("get_article", canonical, "第五十一条") in client.calls
    assert not any(call[0] == "semantic_exact" for call in client.calls)
    assert assess_statute(
        "治安管理处罚条例", "第五十一条", result, [result.trace], []
    )[0].code == StatuteErrorCode.SOURCE_REPEALED


def test_numbered_full_title_retries_short_title_shape_deterministically():
    client = ShortTitleOnlyClient(laws=[
        PkulawLawRecord(
            title="中华人民共和国治安管理处罚条例",
            timeliness=["废止或失效"],
        )
    ])

    result = PkulawFallbackSource(client).lookup(LookupRequest(
        law_title="中华人民共和国治安管理处罚条例",
        article_no="第五十一条",
    ))

    assert result.status == LookupStatus.ARTICLE_FOUND
    assert [call[1] for call in client.calls if call[0] == "get_article"] == [
        "中华人民共和国治安管理处罚条例",
        "治安管理处罚条例",
    ]


def test_numbered_semantic_other_articles_are_ranked_and_limited():
    articles = [
        _article(f"第{i}条", "保护当事人的合法民事权益和财产权利。")
        for i in range(1, 6)
    ]
    result = PkulawFallbackSource(RoutingClient(semantic=articles, laws=[LAW])).lookup(
        _request()
    )
    assert result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
    assert 0 < len(result.evidence.related_articles) <= 3


def test_exact_miss_keeps_semantic_candidates_and_completes_absence_check():
    result = PkulawFallbackSource(RoutingClient(
        semantic=[_article("第二条", "保护当事人的合法民事权益。")],
        laws=[LAW],
    )).lookup(_request("第九条"))

    assert result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
    assert [item.article_no for item in result.evidence.related_articles] == ["第二条"]
    assert result.trace.metadata["article_absent_confirmed"] is True
    assert result.trace.metadata["article_absence_confirmation"] == "single_signal"


def test_numbered_all_article_routes_miss_but_law_exists():
    result = PkulawFallbackSource(RoutingClient(laws=[LAW])).lookup(_request())
    assert result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
    assert result.trace.metadata["article_absent_confirmed"] is True
    assert result.trace.metadata["article_absence_confirmation"] == "single_signal"


def test_short_law_name_uses_unique_unversioned_record_for_existence():
    records = [
        PkulawLawRecord(title="中华人民共和国刑法", timeliness=["已被修改"]),
        PkulawLawRecord(title="中华人民共和国刑法(2011修正)", timeliness=["已被修改"]),
    ]

    result = PkulawFallbackSource(RoutingClient(laws=records)).lookup(
        LookupRequest(law_title="刑法", article_no="第453条")
    )

    assert result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
    assert result.trace.metadata["title"] == "中华人民共和国刑法"


def test_law_item_confirms_or_contradicts_missing_article():
    confirmed = PkulawFallbackSource(LawItemRoutingClient(
        PkulawNotFoundError("未找到数据"), laws=[LAW]
    )).lookup(_request())
    found = PkulawFallbackSource(LawItemRoutingClient(
        _article(), laws=[LAW]
    )).lookup(_request())

    assert confirmed.trace.metadata["article_absence_confirmation"] == "double_signal"
    assert confirmed.trace.metadata["article_absent_confirmed"] is True
    assert found.status == LookupStatus.ARTICLE_FOUND


def test_numbered_all_routes_miss_returns_candidates_and_completed_marker():
    candidate = PkulawLawRecord(title="中华人民共和国民法典总则编司法解释")
    result = PkulawFallbackSource(RoutingClient(laws=[candidate])).lookup(_request())
    assert result.status == LookupStatus.LAW_NOT_FOUND
    assert result.trace.metadata["search_completed"] is True
    assert result.trace.metadata["candidate_titles"] == [candidate.title]


def test_numbered_exact_network_error_falls_back_to_semantic():
    client = RoutingClient(
        exact=PkulawMcpError("network"), semantic=[_article()], laws=[LAW]
    )
    result = PkulawFallbackSource(client).lookup(_request())
    assert result.status == LookupStatus.ARTICLE_FOUND
    assert any(call[0] == "semantic_exact" for call in client.calls)


def test_numbered_semantic_error_keeps_exact_absence_result_and_trace():
    client = RoutingClient(semantic=PkulawMcpError("semantic network"), laws=[LAW])
    result = PkulawFallbackSource(client).lookup(_request())
    assert result.status == LookupStatus.LAW_FOUND_ARTICLE_MISSING
    assert result.trace.metadata["route_attempts"][-2]["status"] == "error"


def test_unnumbered_bare_reference_skips_semantic_search():
    client = RoutingClient(laws=[LAW])
    request = LookupRequest(
        law_title="民法典", context_text="根据《民法典》规定"
    )
    result = PkulawFallbackSource(client).lookup(request)
    assert result.status == LookupStatus.LAW_FOUND_TEXT_UNAVAILABLE
    assert not any(call[0].startswith("semantic") for call in client.calls)


def test_unnumbered_substantive_reference_returns_ranked_articles_and_filters_titles():
    client = RoutingClient(
        laws=[LAW],
        semantic=[
            _article("第一条", "保护当事人的合法民事权益。"),
            _article(
                "第二条", "保护当事人的合法民事权益。", title="中华人民共和国刑法"
            ),
        ],
    )
    request = LookupRequest(
        law_title="民法典",
        context_text="应当保护当事人的合法民事权益和财产权利",
    )
    result = PkulawFallbackSource(client).lookup(request)
    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert [x.article_no for x in result.evidence.related_articles] == ["第一条"]


def test_unnumbered_recognition_fulltext_retrieves_inside_target_law_without_get_article():
    title = "最高人民法院关于审理交通肇事刑事案件具体应用法律若干问题的解释"
    recognized = PkulawRecognizedLaw(
        mentioned_title="关于审理交通肇事刑事案件具体应用法律若干问题的解释",
        canonical_title=title,
        fulltext=(
            "第一条　违反交通运输管理法规发生重大交通事故的，依法处罚。\n\n"
            "第二条　交通肇事致一人以上重伤，负事故全部或者主要责任，并具有"
            "酒后、吸食毒品后驾驶机动车辆情形的，以交通肇事罪定罪处罚。\n\n"
            "第三条　交通运输肇事后逃逸的，依法处罚。"
        ),
        url="https://pkulaw.com/chl/traffic.html",
    )
    law = PkulawLawRecord(title=title, timeliness=["现行有效"])
    client = RecognitionCorpusClient([recognized], laws=[law])

    result = PkulawFallbackSource(client).lookup(LookupRequest(
        law_title="关于审理交通肇事刑事案件具体应用法律若干问题的解释",
        query_text="一人以上重伤；负事故全部或者主要责任；酒后驾驶",
    ))

    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert result.evidence.related_articles[0].article_no == "第二条"
    assert result.trace.metadata["retrieval_method"] == "pkulaw_recognition_article_bm25"
    assert [call[0] for call in client.calls] == ["law_recognition", "law_list"]


def test_unnumbered_normative_document_returns_paragraph_locator_not_fake_article():
    title = "某某工作指导意见"
    client = RecognitionCorpusClient(
        [PkulawRecognizedLaw(
            mentioned_title=title,
            canonical_title=title,
            fulltext="一、总体要求\n\n坚持依法治理。\n\n二、风险处置\n\n建立重大风险报告机制。",
            url="https://pkulaw.com/chl/guidance.html",
        )],
        laws=[PkulawLawRecord(title=title, timeliness=["现行有效"])],
    )

    result = PkulawFallbackSource(client).lookup(LookupRequest(
        law_title=title,
        query_text="重大风险报告机制",
    ))

    excerpt = result.evidence.related_articles[0]
    assert excerpt.article_no == ""
    assert excerpt.locator == "二、风险处置 · 段落2"
    assert excerpt.locator_type == "paragraph"
    assert result.trace.metadata["retrieval_method"] == "pkulaw_recognition_paragraph_bm25"


def test_unnumbered_semantic_article_without_number_has_no_blank_prefix():
    text = "保护当事人的合法民事权益和财产权利。"
    client = RoutingClient(laws=[LAW], semantic=[_article("", text)])
    request = LookupRequest(law_title="民法典", context_text=text)
    result = PkulawFallbackSource(client).lookup(request)
    assert result.status == LookupStatus.RELEVANT_ARTICLES_FOUND
    assert result.evidence.article_text == text


@pytest.mark.parametrize(
    ("semantic", "expected"),
    [
        (
            [_article("第一条", "保护当事人的合法民事权益。")],
            LookupStatus.RELEVANT_ARTICLES_FOUND,
        ),
        ([], LookupStatus.LAW_NOT_FOUND),
    ],
)
def test_unnumbered_keyword_miss_semantic_outcomes(semantic, expected):
    request = LookupRequest(
        law_title="民法典",
        context_text="应当保护当事人的合法民事权益和财产权利",
    )
    result = PkulawFallbackSource(RoutingClient(semantic=semantic)).lookup(request)
    assert result.status == expected


def test_unnumbered_keyword_miss_semantic_error_is_source_error():
    request = LookupRequest(
        law_title="民法典",
        context_text="应当保护当事人的合法民事权益和财产权利",
    )
    result = PkulawFallbackSource(
        RoutingClient(semantic=PkulawMcpError("semantic network"))
    ).lookup(request)
    assert result.status == LookupStatus.SOURCE_ERROR
