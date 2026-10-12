from types import SimpleNamespace

from ccitecheck.orchestration.scheduler import _matching_historical_location
from ccitecheck.domain.citation import ArticleRef
from ccitecheck.domain.statute_results import StatuteLocator, StatuteVersion
from ccitecheck.verification.statutes import (
    LocationStatus,
    assess_location,
    parse_article_structure,
)


ARTICLE_TEXT = """第二条　第一款内容。
第二款引导语：
（一）第一项内容；
（二）第二项内容。
第三款内容。"""


def test_parse_article_structure_preserves_paragraph_and_item_levels():
    structure = parse_article_structure("第二条", ARTICLE_TEXT)

    assert structure is not None
    assert [paragraph.paragraph_no for paragraph in structure.paragraphs] == [
        "第一款", "第二款", "第三款"
    ]
    assert [item.item_no for item in structure.paragraphs[1].items] == [
        "第一项", "第二项"
    ]


def test_location_selects_the_cited_paragraph_only():
    structure = parse_article_structure("第二条", ARTICLE_TEXT)

    assessment = assess_location(
        structure,
        [StatuteLocator(article_no="第二条", paragraph_no="第三款")],
    )

    assert assessment.status == LocationStatus.VALID
    assert assessment.authoritative_text == "第三款内容。"


def test_location_rejects_missing_paragraph():
    structure = parse_article_structure("第二条", ARTICLE_TEXT)

    assessment = assess_location(
        structure,
        [StatuteLocator(article_no="第二条", paragraph_no="第四款")],
    )

    assert assessment.status == LocationStatus.INVALID
    assert "不存在第四款" in assessment.message


def test_location_validates_item_inside_its_paragraph():
    structure = parse_article_structure("第二条", ARTICLE_TEXT)

    valid = assess_location(
        structure,
        [StatuteLocator(article_no="第二条", paragraph_no="第二款", item_no="第二项")],
    )
    invalid = assess_location(
        structure,
        [StatuteLocator(article_no="第二条", paragraph_no="第三款", item_no="第二项")],
    )

    assert valid.authoritative_text == "第二款引导语：\n（二）第二项内容。"
    assert invalid.status == LocationStatus.INVALID
    assert "第三款共0项" in invalid.message


def test_structure_unavailable_does_not_validate_subarticle_locator():
    assessment = assess_location(
        None,
        [StatuteLocator(article_no="第二条", paragraph_no="第二款")],
    )

    assert assessment.status == LocationStatus.STRUCTURE_UNAVAILABLE


def test_flattened_article_does_not_claim_later_paragraph_is_missing():
    structure = parse_article_structure("第二条", "第二条　未保留自然段边界的条文文本。")

    assessment = assess_location(
        structure,
        [StatuteLocator(article_no="第二条", paragraph_no="第二款")],
    )

    assert assessment.status == LocationStatus.STRUCTURE_UNAVAILABLE


def test_historical_version_can_resolve_a_missing_current_paragraph():
    item = SimpleNamespace(
        article_no="第二条",
        article=ArticleRef(article="第二条", paragraphs=["第三款"]),
    )
    historical = StatuteVersion(
        version_key="2018",
        article_no="第二条",
        article_text="第二条　第一款。\n第二款。\n历史第三款。",
    )

    assert _matching_historical_location(item, [historical]) == historical


def test_item_without_paragraph_resolves_within_single_item_paragraph():
    """引用只写"项"未写"款"（如"第五条第一项"，条文为单引言款 + 各项）时，
    应定位到唯一含项的款内对应项，而非因缺款号判为结构不可用。"""
    text = (
        "本法不适用于：\n"
        "（一）法律、法规，国家机关的决议、决定、命令和其他具有立法、行政、司法性质的文件，及其官方正式译文；\n"
        "（二）单纯事实消息；\n"
        "（三）历法、通用数表、通用表格和公式。"
    )
    structure = parse_article_structure("第五条", text)

    valid = assess_location(
        structure,
        [StatuteLocator(article_no="第五条", item_no="第一项")],
    )
    assert valid.status == LocationStatus.VALID
    assert valid.authoritative_text.startswith("本法不适用于：\n（一）法律、法规")

    out_of_range = assess_location(
        structure,
        [StatuteLocator(article_no="第五条", item_no="第九项")],
    )
    assert out_of_range.status == LocationStatus.INVALID
    assert "不存在第九项" in out_of_range.message


def test_local_single_line_article_rejects_out_of_range_paragraph():
    """本地精编库单行条文（真实一款）被引"第二款"时应判定不存在该款，
    而非因边界不可靠退化为结构不可用。"""
    text = "个人信息处理者可以在合理的范围内处理已合法公开的个人信息；个人明确拒绝的除外。个人信息处理者处理已公开的个人信息，对个人权益有重大影响的，应当取得个人同意。"
    structure = parse_article_structure("第二十七条", text, trust_single_paragraph=True)
    assert structure.paragraph_boundaries_reliable is True

    assessment = assess_location(
        structure,
        [StatuteLocator(article_no="第二十七条", paragraph_no="第二款")],
    )
    assert assessment.status == LocationStatus.INVALID
    assert "不存在第二款" in assessment.message


def test_untrusted_single_line_article_stays_structure_unavailable():
    """来源不可信（如法宝压平）时单行条文仍保守判为结构不可用，避免误报。"""
    text = "第一句。第二句。"
    structure = parse_article_structure("第九条", text)
    assert structure.paragraph_boundaries_reliable is False

    assessment = assess_location(
        structure,
        [StatuteLocator(article_no="第九条", paragraph_no="第二款")],
    )
    assert assessment.status == LocationStatus.STRUCTURE_UNAVAILABLE


def test_reference_modifiers_stripped_from_bare_law_name():
    """裸引用的描述性/介词前缀不属于法名：以刑法=刑法，现行反不正当竞争法=反不正当竞争法。"""
    from ccitecheck.recognition.statutes import extract_legal_sources
    assert extract_legal_sources("以刑法第二百八十五条第三款认定非法获取数据罪。")[0].title == "刑法"
    assert extract_legal_sources("现行反不正当竞争法第十三条第三款关注数据获取。")[0].title == "反不正当竞争法"
