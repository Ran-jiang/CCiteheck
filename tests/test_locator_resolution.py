from ccitecheck.domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ccitecheck.verification.statutes import resolve_location_candidates


def _evidence(article_no: str, text: str) -> ArticleEvidence:
    return ArticleEvidence(
        law_title="示例法",
        source_type="law",
        article_no=article_no,
        article_text=text,
        data_source=SourceTrace(
            tier=SourceTier.PKULAW_FALLBACK,
            source_name="北大法宝",
            source_url="https://example.test/law",
            status=LookupStatus.RELEVANT_ARTICLES_FOUND,
        ),
    )


def test_unique_verbatim_mcp_candidate_resolves_to_paragraph():
    resolution = resolve_location_candidates(
        "劳动者依法享有休息和休假的权利。",
        [_evidence("第二条", "第一款其他内容。\n劳动者依法享有休息和休假的权利。")],
    )

    assert resolution.status == "resolved"
    assert resolution.candidates[0].locator.article_no == "第二条"
    assert resolution.candidates[0].locator.paragraph_no == "第二款"


def test_multiple_verbatim_candidates_remain_pending():
    resolution = resolve_location_candidates(
        "共同内容。",
        [
            _evidence("第二条", "共同内容。"),
            _evidence("第三条", "共同内容。"),
        ],
    )

    assert resolution.status == "candidates_pending"
    assert len(resolution.candidates) == 2


def test_paraphrase_is_not_forced_into_a_location():
    resolution = resolve_location_candidates(
        "文书进行了概括转述。",
        [_evidence("第二条", "权威原文使用了完全不同的表述。")],
    )

    assert resolution.status == "not_found"


def test_exact_supported_candidate_beats_unsupported_overlap():
    resolution = resolve_location_candidates(
        "合同是民事主体之间设立、变更、终止民事法律关系的协议。",
        [
            _evidence("第一百三十三条", "民事法律行为是民事主体设立、变更、终止民事法律关系的行为。"),
            _evidence("第四百六十四条", "合同是民事主体之间设立、变更、终止民事法律关系的协议。"),
        ],
        cited_article_no="第四百六十三条",
    )

    assert resolution.status == "resolved"
    assert resolution.candidates[0].locator.article_no == "第四百六十四条"


def test_unique_strong_article_can_resolve_without_claiming_full_text_support():
    resolution = resolve_location_candidates(
        "公司向其他企业投资或者为他人提供担保，依照公司章程的规定，由董事会或者股东会、股东大会决议。",
        [_evidence("第十五条", "公司向其他企业投资或者为他人提供担保，按照公司章程的规定，由董事会或者股东会决议。")],
        cited_article_no="第十六条",
    )

    assert resolution.status == "resolved"
    assert resolution.candidates[0].locator.article_no == "第十五条"
    assert resolution.candidates[0].supported is False
    assert resolution.candidates[0].confirmed_level == "article"


def test_unique_strong_paragraph_resolves_minor_paraphrase():
    resolution = resolve_location_candidates(
        "训练数据包含个人信息的，应当遵守个人信息保护相关规则",
        [_evidence(
            "第十四条",
            "训练数据包含个人信息的，应当遵守个人信息保护的有关规定。\n"
            "提供生物识别信息编辑功能的，应当取得个人单独同意。",
        )],
        cited_article_no="第十四条",
    )

    assert resolution.status == "resolved"
    assert resolution.candidates[0].locator.paragraph_no == "第一款"
    assert resolution.candidates[0].confirmed_level == "paragraph"


def test_distinctive_clause_beats_similar_penalty_candidates():
    resolution = resolve_location_candidates(
        "盗窃公私财物数额较大的，处三年以下有期徒刑、拘役或者管制，并处或者单处罚金。",
        [
            _evidence("第二百六十四条", "盗窃公私财物，数额较大的，处三年以下有期徒刑、拘役或者管制，并处或者单处罚金；数额巨大依法从重处罚。"),
            _evidence("第二百六十六条", "诈骗公私财物，数额较大的，处三年以下有期徒刑、拘役或者管制，并处或者单处罚金。"),
            _evidence("第二百七十四条", "敲诈勒索公私财物，数额较大的，处三年以下有期徒刑、拘役或者管制，并处或者单处罚金。"),
        ],
        cited_article_no="第二百六十五条",
    )

    assert resolution.status == "resolved"
    assert resolution.candidates[0].locator.article_no == "第二百六十四条"


def test_item_condition_may_precede_parent_conclusion():
    text = (
        "有下列情形之一，调解无效的，应当准予离婚：\n"
        "（一）重婚或者与他人同居；\n"
        "（二）实施家庭暴力；\n"
        "（三）有赌博、吸毒等恶习屡教不改；\n"
        "（四）因感情不和分居满二年。"
    )
    resolution = resolve_location_candidates(
        "因感情不和分居满二年，调解无效的，应当准予离婚。",
        [_evidence("第一千零七十九条", text)],
        cited_article_no="第一千零七十九条",
    )

    assert resolution.status == "resolved"
    assert resolution.candidates[0].locator.item_no == "第四项"


def test_reordered_item_matching_keeps_the_distinctive_item_number():
    resolution = resolve_location_candidates(
        "一方因受到人身损害获得的赔偿或者补偿，为夫妻一方的个人财产。",
        [_evidence(
            "第一千零六十三条",
            "下列财产为夫妻一方的个人财产：\n（一）一方的婚前财产；\n（二）一方因受到人身损害获得的赔偿或者补偿。",
        )],
        cited_article_no="第一千零六十三条",
    )

    assert resolution.status == "resolved"
    assert resolution.candidates[0].locator.item_no == "第二项"
