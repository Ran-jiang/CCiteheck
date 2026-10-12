"""承前引用（同条第X款/第五款）的跨句定位兜底。"""

import pytest

from ccitecheck.domain.citation import (
    ArticleRef,
    Claim,
    ClaimType,
    LegalSource,
    LegalSourceClaimEntities,
)
from ccitecheck.domain.citation_integrity import validate_claim_citation_integrity
from ccitecheck.recognition.spans import locate_claim_article_spans
from ccitecheck.recognition.statutes import extract_legal_sources


def _claim(text: str, articles: list[ArticleRef]) -> Claim:
    return Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(
            legal_sources=[
                LegalSource(
                    title="中华人民共和国反不正当竞争法",

                    articles=articles,
                )
            ]
        ),
    )


def test_carryforward_paragraph_reference_is_located():
    text = (
        "《中华人民共和国反不正当竞争法》第十三条第三款规定，经营者不得破坏技术管理措施。"
        "同条第四款规定经营者不得滥用平台规则。第五款进一步规定其他情形。"
    )
    claim = _claim(text, [
        ArticleRef(article="第十三条", paragraphs=["第三款"]),
        ArticleRef(article="第十三条", paragraphs=["第四款"]),
        ArticleRef(article="第十三条", paragraphs=["第五款"]),
    ])
    locate_claim_article_spans(claim)

    citations = claim.entities.citations
    assert [item.span_status for item in citations] == ["located"] * 3
    assert [item.role for item in citations] == [
        "direct", "carry_forward", "carry_forward",
    ]
    assert [item.locator.paragraph for item in citations] == [
        "第三款", "第四款", "第五款",
    ]


def test_citation_without_local_proposition_still_uses_full_claim_text():
    text = "依据《中华人民共和国反不正当竞争法》第十三条。"
    claim = _claim(text, [ArticleRef(article="第十三条")])
    locate_claim_article_spans(claim)

    assert claim.text == text
    assert not hasattr(claim.entities.citations[0], "verification")


def _claim_from_text(text: str) -> Claim:
    return Claim(
        claim_id="cl_00001",
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        text=text,
        anchor_ids=["line00001"],
        entities=LegalSourceClaimEntities(legal_sources=extract_legal_sources(text)),
    )


@pytest.mark.parametrize("separator", ["-", "—", "–", "~", "～", "至"])
def test_compact_article_ranges_have_one_occurrence_per_article(separator):
    claim = _claim_from_text(f"依据《消防法》58{separator}69条承担相应责任。")

    locate_claim_article_spans(claim)

    assert [item.locator.article for item in claim.entities.citations] == [
        f"第{number}条" for number in range(58, 70)
    ]
    assert validate_claim_citation_integrity(claim) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "《商业银行法》第27条第（四）项明确规定。",
            [("第27条", None, "第四项")],
        ),
        (
            "《商业银行法》第27条第一款第（四）项明确规定。",
            [("第27条", "第一款", "第四项")],
        ),
        (
            "《商业银行法》第27条第一款、第二款明确规定。",
            [("第27条", "第一款", None), ("第27条", "第二款", None)],
        ),
        (
            "《商业银行法》第27条第（一）项、第（二）项明确规定。",
            [("第27条", None, "第一项"), ("第27条", None, "第二项")],
        ),
        (
            "《任职资格管理办法》第8条第（一）项及第10条第（一）项均有规定。",
            [("第8条", None, "第一项"), ("第10条", None, "第一项")],
        ),
        (
            "《任职资格管理办法》第六条第（六）项、第（七）项规定。",
            [("第六条", None, "第六项"), ("第六条", None, "第七项")],
        ),
        (
            "著作权法第五条第一项排除保护。",
            [("第五条", None, "第一项")],
        ),
    ],
)
def test_recognized_article_paragraph_and_item_forms_have_occurrences(text, expected):
    claim = _claim_from_text(text)

    locate_claim_article_spans(claim)

    assert [
        (item.locator.article, item.locator.paragraph, item.locator.item)
        for item in claim.entities.citations
    ] == expected
    assert validate_claim_citation_integrity(claim) == []


def test_inherited_item_uses_the_single_inherited_parent_paragraph():
    claim = _claim(
        "第二项规定，商品性能信息不得失实。",
        [ArticleRef(article="第二十八条", paragraphs=["第二款"], items=["第二项"])],
    )

    locate_claim_article_spans(claim)

    assert [
        (item.locator.article, item.locator.paragraph, item.locator.item)
        for item in claim.entities.citations
    ] == [("第二十八条", "第二款", "第二项")]
    assert validate_claim_citation_integrity(claim) == []
