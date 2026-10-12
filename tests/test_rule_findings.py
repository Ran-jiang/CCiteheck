"""确定性规则判定与识别质量修复的单元测试（不依赖网络）。"""

from ccitecheck.recognition.cases import NAMED_CASE_PATTERN, extract_case_refs
from ccitecheck.recognition.statutes import _extract_articles_from_text
from ccitecheck.infrastructure.database import strip_version_annotation
from ccitecheck.verification.statutes import (
    assess_statute,
    classify_not_verifiable,
    suggest_similar_title,
)
from ccitecheck.domain.evidence import (
    ArticleEvidence,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from ccitecheck.domain.statute_results import StatuteErrorCode
from ccitecheck.retrieval.sources import LookupResult


# ---------- D1 案例正则 ----------

def test_named_case_pattern_ignores_common_false_positives():
    negatives = [
        "业务部门应持续关注相关舆情与投诉情况，及时反馈群体性维权情况，对集体诉讼、民事诉讼做出预案。",
        "被诉侵权产品与涉案专利不构成相同。",
        "消费者可以提起诉讼或申请仲裁。",
        "上诉人对一审判决不服提起上诉。",
    ]
    for text in negatives:
        assert NAMED_CASE_PATTERN.search(text) is None, text


def test_named_case_pattern_matches_real_case_names():
    positives = [
        "甲公司诉乙公司买卖合同纠纷案",
        "斯广树诉天津联通电信服务合同纠纷案",
        "王老吉诉加多宝虚假宣传案",
    ]
    for text in positives:
        assert NAMED_CASE_PATTERN.search(text) is not None, text


def test_named_case_validation_rejects_statute_text_polluted_by_case_words():
    texts = [
        "《民诉解释》第392条明确规定：“民事诉讼法第207条（现为第211条）第13项规定的审判人员审理该案件时有贪污受贿行为。”",
        "B、C、D选项：《民诉解释》第286条规定：“人民法院受理公益诉讼案件，不影响受害人依法提起诉讼。”",
    ]

    for text in texts:
        assert extract_case_refs(text) == [], text


def test_named_case_validation_preserves_supported_case_name_variants():
    texts = {
        "甲公司诉乙公司买卖合同纠纷案": "甲公司诉乙公司买卖合同纠纷案",
        "参见斯广树诉天津联通电信服务合同纠纷案": "斯广树诉天津联通电信服务合同纠纷案",
        "案例：王老吉诉加多宝虚假宣传案": "王老吉诉加多宝虚假宣传案",
        "张三诉李四案": "张三诉李四案",
        "甲公司诉乙公司、丙公司合同纠纷案": "甲公司诉乙公司、丙公司合同纠纷案",
    }

    for text, expected in texts.items():
        refs = extract_case_refs(text)
        assert [ref.case_name for ref in refs] == [expected], text


# ---------- D2 版本注记剥离 ----------

def test_strip_version_annotation():
    assert strip_version_annotation("网络安全法（2025修正）") == "网络安全法"
    assert strip_version_annotation("公司法(2023修订)") == "公司法"
    assert strip_version_annotation("认定方法（试行）") == "认定方法（试行）"


# ---------- D3 条号范围展开 ----------

def test_article_range_expansion_fills_middle_articles():
    refs = _extract_articles_from_text("违反本法第四十三条至第四十五条规定")
    articles = {r.article for r in refs}
    assert {"第四十三条", "第四十四条", "第四十五条"} <= articles


# ---------- D4 不可核验文件分类 ----------

def testclassify_not_verifiable():
    assert classify_not_verifiable("互联网应用程序个人信息收集使用规定（征求意见稿）")
    assert classify_not_verifiable("信息安全技术 个人信息安全规范（GB/T 35273-2020）")
    assert classify_not_verifiable("中华人民共和国民法典") is None
    assert classify_not_verifiable("App违法违规收集使用个人信息行为认定方法") is None


# ---------- A1 条文不存在 ----------

def _local_partial_result(law_title: str, article_no: str, article_count: int):
    trace = SourceTrace(
        tier=SourceTier.LOCAL_SQLITE,
        source_name="本地库",
        status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
        metadata={"local_article_count": article_count},
    )
    evidence = ArticleEvidence(
        law_title=law_title,
        source_type="law",
        article_no=article_no,
        data_source=trace,
    )
    return LookupResult(trace.status, evidence, trace), [trace]


def test_article_count_alone_does_not_produce_high_finding():
    result, attempts = _local_partial_result("中华人民共和国民法典", "第一千三百条", 1260)
    findings = assess_statute(
        "中华人民共和国民法典", "第一千三百条", result, attempts, []
    )
    assert any(
        f.code == StatuteErrorCode.ARTICLE_NOT_FOUND
        and f.risk_level == "MEDIUM"
        for f in findings
    )


# ---------- A2 废止检测 ----------

def test_repealed_law_produces_outdated_finding():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.LAW_FOUND_ARTICLE_MISSING,
    )
    evidence = ArticleEvidence(
        law_title="中华人民共和国合同法",
        source_type="law",
        version_status="废止或失效",
        data_source=trace,
    )
    result = LookupResult(trace.status, evidence, trace)
    findings = assess_statute(
        "中华人民共和国合同法", "第五十二条", result, [trace], []
    )
    assert any(
        f.code == StatuteErrorCode.SOURCE_REPEALED
        and f.risk_level == "HIGH"
        for f in findings
    )
    # 已废止时不应叠加"未检索到条文"噪音
    assert not any(
        f.code == StatuteErrorCode.ARTICLE_NOT_FOUND for f in findings
    )


# ---------- A3 法源不存在与法名纠错 ----------

def test_law_not_found_after_completed_search_produces_high_finding():
    trace = SourceTrace(
        tier=SourceTier.PKULAW_FALLBACK,
        source_name="北大法宝",
        status=LookupStatus.LAW_NOT_FOUND,
        metadata={"search_completed": True},
    )
    result = LookupResult(LookupStatus.LAW_NOT_FOUND, None, trace)
    findings = assess_statute(
        "中华人民共和国印章管理办法", "第五条", result, [trace], []
    )
    assert any(
        f.code == StatuteErrorCode.SOURCE_NOT_FOUND
        and f.risk_level == "HIGH"
        for f in findings
    )


def testsuggest_similar_title_catches_one_char_typo():
    suggestion = suggest_similar_title(
        "消费者召回管理暂行规定", ["消费品召回管理暂行规定", "中华人民共和国民法典"]
    )
    assert suggestion == "消费品召回管理暂行规定"


def testsuggest_similar_title_rejects_weak_matches():
    suggestion = suggest_similar_title(
        "中华人民共和国印章管理办法", ["中华人民共和国税收征收管理法"]
    )
    assert suggestion is None
