from ccitecheck.orchestration.cases import _match_case_record, _same_court
from ccitecheck.retrieval.sources.pkulaw.client import PkulawCaseRecord


def test_specific_court_alias_is_equivalent():
    assert _same_court("重庆一中院", "重庆市第一中级人民法院")


def test_broad_region_court_is_not_a_specific_court_alias():
    assert not _same_court("重庆法院", "重庆市第一中级人民法院")


def test_duplicate_exact_titles_require_a_unique_matching_court():
    records = [
        PkulawCaseRecord(title="甲诉乙合同纠纷案", court="重庆市第一中级人民法院"),
        PkulawCaseRecord(title="甲诉乙合同纠纷案", court="重庆市第二中级人民法院"),
    ]

    match, basis, conflict = _match_case_record(None, "甲诉乙合同纠纷案", "重庆一中院", records)

    assert match is records[0]
    assert basis == "exact_title_and_court"


def test_duplicate_exact_titles_do_not_accept_a_broad_region_court():
    records = [
        PkulawCaseRecord(title="甲诉乙合同纠纷案", court="重庆市第一中级人民法院"),
        PkulawCaseRecord(title="甲诉乙合同纠纷案", court="重庆市第二中级人民法院"),
    ]

    match, basis, conflict = _match_case_record(None, "甲诉乙合同纠纷案", "重庆法院", records)

    assert match is None
    assert basis is None


def test_duplicate_supplier_records_with_same_number_court_and_date_are_one_case():
    records = [
        PkulawCaseRecord(
            title="李某诉周某案",
            case_number="（2019）京73民终225号",
            court="北京知识产权法院",
            last_instance_date="2020.05.26",
            url="https://example.com/a",
        ),
        PkulawCaseRecord(
            title="李霞与周梅森案",
            case_number="（2019）京73民终225号",
            court="北京知识产权法院",
            last_instance_date="2020.05.26",
            holding="裁判观点",
            url="https://example.com/b",
        ),
    ]

    match, basis, conflict = _match_case_record("(2019)京73民终225号", None, None, records)

    assert match is records[1]
    assert basis == "exact_case_number_cluster"


def test_catalog_entry_missing_date_does_not_block_cluster():
    records = [
        PkulawCaseRecord(
            title="优衣库商贸有限公司与广州市指南针会展服务有限公司侵害商标权纠纷再审民事判决书",
            case_number="（2018）最高法民再396号",
            court="最高人民法院",
            last_instance_date="2018.12.28",
            url="https://example.com/a",
        ),
        PkulawCaseRecord(
            title="2018年中国法院10大知识产权案件之三：“优衣库”侵害商标权纠纷案",
            case_number="（2018）最高法民再396号",
            court="最高人民法院",
            last_instance_date=None,
            holding="裁判要旨",
            url="https://example.com/b",
        ),
    ]

    match, basis, conflict = _match_case_record("(2018)最高法民再396号", None, None, records)

    assert match is records[1]  # 择优取有要旨的条目
    assert basis == "exact_case_number_cluster"
    assert conflict is None


def test_cited_document_type_selects_matching_record():
    records = [
        PkulawCaseRecord(
            title="甲与乙合同纠纷再审民事判决书",
            case_number="（2018）最高法民再396号",
            court="最高人民法院",
            last_instance_date="2018.12.28",
        ),
        PkulawCaseRecord(
            title="甲与乙合同纠纷中止诉讼民事裁定书",
            case_number="（2018）最高法民再396号",
            court="最高人民法院",
            last_instance_date="2018.06.01",
        ),
    ]

    match, basis, conflict = _match_case_record(
        "(2018)最高法民再396号", None, None, records, "裁定书",
    )

    assert match is records[1]
    assert basis == "exact_case_number_cited_document_type"
    assert conflict is None


def test_uncited_document_type_defaults_to_judgment():
    records = [
        PkulawCaseRecord(
            title="甲与乙合同纠纷再审民事判决书",
            case_number="（2018）最高法民再396号",
            court="最高人民法院",
            last_instance_date="2018.12.28",
        ),
        PkulawCaseRecord(
            title="甲与乙合同纠纷中止诉讼民事裁定书",
            case_number="（2018）最高法民再396号",
            court="最高人民法院",
            last_instance_date="2018.06.01",
        ),
    ]

    match, basis, conflict = _match_case_record("(2018)最高法民再396号", None, None, records)

    assert match is records[0]
    assert basis == "exact_case_number_default_judgment"
    assert conflict is None


def test_cited_type_absent_from_records_reports_specific_conflict():
    records = [
        PkulawCaseRecord(
            title="甲与乙合同纠纷中止诉讼民事裁定书",
            case_number="（2018）最高法民再396号",
            court="最高人民法院",
            last_instance_date="2018.06.01",
        ),
        PkulawCaseRecord(
            title="甲与乙合同纠纷恢复审理民事裁定书",
            case_number="（2018）最高法民再396号",
            court="最高人民法院",
            last_instance_date="2018.08.01",
        ),
    ]

    match, basis, conflict = _match_case_record(
        "(2018)最高法民再396号", None, None, records, "判决书",
    )

    assert match is None
    assert conflict is not None
    assert "判决书" in conflict and "裁定书" in conflict


def test_same_type_conflicting_dates_report_dates_in_conflict():
    records = [
        PkulawCaseRecord(
            title="甲与乙合同纠纷一审民事判决书",
            case_number="（2018）最高法民再396号",
            court="最高人民法院",
            last_instance_date="2018.06.01",
        ),
        PkulawCaseRecord(
            title="甲与乙合同纠纷再审民事判决书",
            case_number="（2018）最高法民再396号",
            court="最高人民法院",
            last_instance_date="2018.12.28",
        ),
    ]

    match, basis, conflict = _match_case_record("(2018)最高法民再396号", None, None, records)

    assert match is None
    assert conflict is not None
    assert "2018.06.01" in conflict and "2018.12.28" in conflict


def test_conflicting_courts_report_courts_in_conflict():
    records = [
        PkulawCaseRecord(
            title="甲与乙合同纠纷二审民事判决书",
            case_number="（2018）云01民终4767号",
            court="云南省昆明市中级人民法院",
            last_instance_date="2018.08.22",
        ),
        PkulawCaseRecord(
            title="甲与乙合同纠纷二审民事判决书",
            case_number="（2018）云01民终4767号",
            court="云南省高级人民法院",
            last_instance_date="2018.08.22",
        ),
    ]

    match, basis, conflict = _match_case_record("(2018)云01民终4767号", None, None, records)

    assert match is None
    assert conflict is not None
    assert "云南省昆明市中级人民法院" in conflict and "云南省高级人民法院" in conflict


def test_recognition_captures_document_type_after_case_number():
    from ccitecheck.recognition.cases import extract_case_refs

    refs = extract_case_refs("参见（2018）最高法民再396号民事判决书的相关认定。")
    assert refs[0].case_number == "（2018）最高法民再396号"
    assert refs[0].document_type == "判决书"

    refs = extract_case_refs("在（2018）最高法民再396号案中，法院认为……")
    assert refs[0].document_type is None


def test_numbered_case_queries_by_number_alone_first():
    """有案号时应先单用案号检索（案号唯一），不与可能脱敏/有误的案名做
    AND，否则法宝取交集会漏检。"""
    from ccitecheck.orchestration.cases import _exact_queries
    from types import SimpleNamespace

    claim = SimpleNamespace(text="……", claim_type=None)
    ref = SimpleNamespace(
        case_number="（2021）京73民终1011号",
        case_name="某网络信息技术有限公司诉某信息科技有限公司不正当竞争纠纷案",
        court=None,
    )
    queries = _exact_queries(claim, ref)
    assert queries[0] == ("", "（2021）京73民终1011号")  # 首选：单用案号
