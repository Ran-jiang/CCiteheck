"""Scheduler 控制的案例检索与证据比较流程。"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..domain.citation import ClaimDocument, ClaimType
from ..domain.display_groups import display_group_id
from ..domain.evidence import CaseEvidence, CaseLookupStatus, CaseSourceTrace
from ..domain.case_results import (
    CaseCandidate,
    CaseErrorCode,
    CaseFinding,
    CaseHoldingCheck,
    CaseVerificationResult,
)
from ..domain.checks import ExecutionStatus
from ..domain.revisions import RevisionProposal
from ..infrastructure.debug_timing import bind_current_timer, measure
from ..query_construction.strategies.common import build_case_keyword_query, build_case_semantic_query
from ..retrieval.sources.pkulaw.cases import CaseSearcher
from ..retrieval.sources.pkulaw.client import (
    PkulawCaseRecord,
    PkulawMcpError,
    PkulawNotConfiguredError,
    PkulawNotFoundError,
)
from ..verification.cases.identity import (
    case_parties as _case_parties,
    case_search_title as _case_search_title,
    equivalent_case_name as _equivalent_case_name,
    compare_case_identity,
    guiding_case_id as _guiding_case_id,
    normalize_case_name as _normalize_case_name,
    normalize_case_number as _normalize_case_number,
    normalize_court as _normalize_court,
    same_court as _same_court,
)

_CASE_LOOKUP_WORKERS = 4
@dataclass(frozen=True)
class _RouteOutcome:
    records: list[PkulawCaseRecord]
    trace: CaseSourceTrace
    completed: bool


def verify_case_claims(
    claim_doc: ClaimDocument,
    searcher: CaseSearcher,
    semantic_checker=None,
) -> list[CaseVerificationResult]:
    """按精准检索、语义补查的顺序核查案例引用。"""
    refs = _unique_case_refs(claim_doc)
    if not refs:
        return []
    with ThreadPoolExecutor(max_workers=_CASE_LOOKUP_WORKERS) as pool:
        outcomes = list(
            pool.map(
                bind_current_timer(
                    lambda pair: _verify_case_reference(
                        searcher, pair[0], pair[1]
                    )
                ),
                refs,
            )
        )
    checks: list[CaseVerificationResult] = []
    for next_id, ((claim, ref), outcome) in enumerate(zip(refs, outcomes), start=1):
        status, evidence, message, traces, candidates = outcome
        holding_check = (
            _check_holding(claim, evidence, semantic_checker)
            if status == CaseLookupStatus.VERIFIED
            else None
        )
        findings = list(holding_check.findings) if holding_check else []
        if evidence is not None:
            identity = compare_case_identity(
                cited_case_number=ref.case_number,
                cited_case_name=ref.case_name,
                cited_court=ref.court,
                evidence=evidence,
            )
            if identity is not None:
                identity.revision = _case_identity_revision(claim.text, ref, evidence, identity.suggestion)
                findings.insert(0, identity)
        elif status == CaseLookupStatus.NOT_FOUND:
            findings.append(CaseFinding(
                code=CaseErrorCode.CASE_NOT_FOUND,
                risk_level="HIGH",
                summary=message,
                suggestion="北大法宝未检索到所引案例，请核对案名、案号、法院或裁判日期。",
            ))
        checks.append(CaseVerificationResult(
            check_id=f"cc_{next_id:05d}",
            display_group_id=display_group_id(claim),
            claim_id=claim.claim_id,
            claim_text=claim.text,
            jurisdiction=ref.jurisdiction,
            source_locations=list(claim.source_locations),
            note_context=claim.note_context,
            cited_case_number=ref.case_number,
            cited_case_name=ref.case_name,
            cited_court=ref.court,
            lookup_status=status,
            evidence=evidence,
            message=message,
            source_attempts=traces,
            candidate_cases=candidates,
            findings=findings,
            outcome=("issue" if findings or status == CaseLookupStatus.NOT_FOUND else "pass" if status == CaseLookupStatus.VERIFIED else "bug"),
            holding_check=holding_check,
        ))
    return checks


def _unique_case_refs(claim_doc: ClaimDocument):
    result = []
    seen: set[tuple[str, str]] = set()
    for claim in claim_doc.claims:
        for ref in getattr(claim.entities, "case_refs", []):
            identity = _normalize_case_number(ref.case_number or "") or _normalize_case_name(ref.case_name or "")
            key = (claim.claim_id, identity)
            if key not in seen:
                seen.add(key)
                result.append((claim, ref))
    return result


def _verify_case_reference(searcher: CaseSearcher, claim, ref):
    if ref.jurisdiction != "CN":
        message = "该案例属于当前不支持的外国法域，请人工核验。"
        trace = CaseSourceTrace(
            source_name="CCiteCheck 案例法域分类",
            status=CaseLookupStatus.OUT_OF_SCOPE,
            message=message,
        )
        return CaseLookupStatus.OUT_OF_SCOPE, None, message, [trace], []
    queries = _exact_queries(claim, ref)
    if not any(title or fulltext for title, fulltext in queries):
        message = "现有引用信息不足以构造案例检索条件。"
        trace = CaseSourceTrace(
            source_name="CCiteheck 案例检索路由",
            status=CaseLookupStatus.MANUAL_REVIEW,
            message=message,
        )
        return CaseLookupStatus.MANUAL_REVIEW, None, message, [trace], []

    traces = []
    exact = None
    for title, fulltext in queries:
        exact = _run_route(
            "北大法宝 MCP：get_case_list",
            lambda title=title, fulltext=fulltext: searcher.search_keyword(title, fulltext),
            {"route": "get_case_list", "title_query": title, "fulltext_query": fulltext},
        )
        traces.append(exact.trace)
        if not exact.completed:
            return exact.trace.status, None, exact.trace.message, traces, []
        if exact.records:
            break

    document_type = getattr(ref, "document_type", None)
    match, basis, conflict = _match_case_record(
        ref.case_number, ref.case_name, ref.court, exact.records, document_type,
    )
    if match is not None:
        if claim.claim_type == ClaimType.CASE_HOLDING_PARAPHRASE and not match.holding:
            supplemented = _semantic_route(searcher, claim, ref)
            traces.append(supplemented.trace)
            if not supplemented.completed:
                return supplemented.trace.status, None, supplemented.trace.message, traces
            same = _same_case(match, supplemented.records)
            if same is not None and same.holding:
                match = same
        return (*_verified(match, ref.case_name, basis, traces), [])

    semantic = _semantic_route(searcher, claim, ref)
    traces.append(semantic.trace)
    if not semantic.completed:
        return semantic.trace.status, None, semantic.trace.message, traces, []

    match, basis, semantic_conflict = _match_case_record(
        ref.case_number, ref.case_name, ref.court, semantic.records, document_type,
    )
    conflict = conflict or semantic_conflict
    if match is None:
        match = _cross_route_match(exact.records, semantic.records)
        basis = "cross_route_case_number" if match is not None else None
    if match is not None:
        return (*_verified(match, ref.case_name, basis, traces), [])

    candidates = _unique_records([*exact.records, *semantic.records])
    if candidates:
        metadata = [_candidate_metadata(record) for record in candidates[:10]]
        traces[-1].metadata["candidates"] = metadata
        message = (
            f"{conflict}，请人工确认引用的是哪一份文书。可参考候选如下。"
            if conflict
            else "北大法宝返回了案例，但现有引用信息不足以证明是哪一份裁判文书。可参考案例如下。"
        )
        return (
            CaseLookupStatus.MANUAL_REVIEW,
            None,
            message,
            traces,
            [CaseCandidate.model_validate(item) for item in metadata],
        )
    message = "北大法宝精准检索和语义检索均未找到文书引用的案例，请核对案名、案号、法院或裁判日期。"
    return CaseLookupStatus.NOT_FOUND, None, message, traces, []


def _exact_queries(claim, ref) -> list[tuple[str, str]]:
    """按优先级排列的精准检索词；前一组未命中记录时才尝试下一组。"""
    title = _case_search_title(ref.case_name or "")
    if ref.case_number:
        # 案号是唯一标识，单独用案号检索；与文书案名做 AND 会因案名脱敏/
        # 有误而漏检（法宝对 title+fulltext 取交集）。案名仅用于随后的身份比对。
        number = ref.case_number.strip()
        attempts = [("", number)]
        if title:
            attempts.append((title, number))
        return attempts
    if _guiding_case_id(_normalize_case_name(ref.case_name or "")):
        return [(title, "")]
    parties = _case_parties(ref.case_name or "")
    if parties:
        attempts = [(title, "")] if title else []
        attempts.append(("", " ".join(parties)))
        return attempts
    _, fulltext = build_case_keyword_query(ref.case_name, claim.text, ref.court)
    return [(title, fulltext)]


def _semantic_route(searcher, claim, ref) -> _RouteOutcome:
    query = build_case_semantic_query(ref.case_name, claim.text, ref.court)
    return _run_route(
        "北大法宝 MCP：search_case",
        lambda: searcher.search_semantic(query),
        {"route": "search_case", "semantic_query": query},
    )


def _run_route(source_name, operation, metadata) -> _RouteOutcome:
    try:
        records = operation()
    except PkulawNotFoundError:
        records = []
    except PkulawMcpError as exc:
        status = _case_error_status(exc)
        detail = str(exc)
        return _RouteOutcome([], CaseSourceTrace(
            source_name=source_name,
            status=status,
            message=(
                detail
                if status == CaseLookupStatus.SOURCE_NOT_CONFIGURED
                else f"数据源调用失败：{detail}"
            ),
            metadata={**metadata, "error": str(exc)},
        ), False)
    status = CaseLookupStatus.MANUAL_REVIEW if records else CaseLookupStatus.NOT_FOUND
    return _RouteOutcome(records, CaseSourceTrace(
        source_name=source_name,
        status=status,
        message=("检索完成，返回案例候选" if records else "检索完成，未命中案例"),
        metadata={**metadata, "candidate_count": len(records)},
    ), True)


def _match_case_record(case_number, case_name, court, records, document_type=None):
    """返回 (确认记录, 匹配依据, 转人工原因)；三者最多一组非空。"""
    if case_number:
        target = _normalize_case_number(case_number)
        matches = [record for record in records if _normalize_case_number(record.case_number) == target]
        if len(matches) == 1:
            return matches[0], "exact_case_number", None
        if matches:
            resolved, basis, conflict = _resolve_duplicate_number_records(matches, document_type)
            if resolved is not None:
                return resolved, basis, None
            return None, None, conflict
    target_title = _normalize_case_name(case_name or "")
    if target_title:
        matches = [record for record in records if _normalize_case_name(record.title) == target_title]
        if len(matches) > 1 and court:
            matches = [record for record in matches if _same_court(court, record.court)]
            if len(matches) == 1:
                return matches[0], "exact_title_and_court", None
        if len(matches) == 1:
            return matches[0], "exact_case_title", None
        contains = [
            record for record in records
            if (
                target_title
                and "典型案例" in record.title
                and target_title in _normalize_case_name(record.title)
            )
        ]
        if len(contains) == 1:
            return contains[0], "contained_case_title", None
        guiding_id = _guiding_case_id(target_title)
        if guiding_id:
            matches = [record for record in records if _guiding_case_id(_normalize_case_name(record.title)) == guiding_id]
            if len(matches) == 1:
                return matches[0], "exact_case_title", None
        parties = _case_parties(case_name or "")
        if parties and _safe_natural_person_pair(parties):
            matches = [
                record for record in records
                if all(party in _normalize_case_name(record.title) for party in parties)
            ]
            if len(matches) == 1:
                return matches[0], "unique_party_pair", None
    return None, None, None


_RECORD_DOC_TYPE = re.compile(r"(判决书|裁定书|调解书|决定书|支付令)")


def _record_document_type(title: str) -> str | None:
    matches = _RECORD_DOC_TYPE.findall(title or "")
    return matches[-1] if matches else None


def _resolve_duplicate_number_records(records, cited_type):
    """同案号多条记录的归并确认。

    案号标识案件而非文书，同案号下可能并存判决书、裁定书等不同文书，
    也可能只是同一份文书的重复编目（含无类型后缀的典型案例条目）。
    先按标题中的文书类型分组选定目标组，组内做非空字段冲突校验后择优。
    返回 (确认记录, 依据, None) 或 (None, None, 转人工原因)。
    """
    groups: dict[str, list[PkulawCaseRecord]] = {}
    untyped: list[PkulawCaseRecord] = []
    for record in records:
        record_type = _record_document_type(record.title)
        if record_type:
            groups.setdefault(record_type, []).append(record)
        else:
            untyped.append(record)
    if cited_type and groups and cited_type not in groups:
        available = "、".join(sorted(groups))
        return None, None, (
            f"文书写明引用的是{cited_type}，但北大法宝同案号下仅收录{available}，"
            "未找到该类型的文书"
        )
    if cited_type and cited_type in groups:
        pool, basis = groups[cited_type] + untyped, "exact_case_number_cited_document_type"
    elif len(groups) > 1:
        if "判决书" not in groups:
            available = "、".join(sorted(groups))
            return None, None, (
                f"同案号下收录了{available}多种文书，文中未写明引用类型，"
                "且没有可默认的判决书"
            )
        pool, basis = groups["判决书"] + untyped, "exact_case_number_default_judgment"
    elif groups:
        pool, basis = next(iter(groups.values())) + untyped, "exact_case_number_cluster"
    else:
        pool, basis = untyped, "exact_case_number_cluster"
    conflict = _cluster_conflict(pool)
    if conflict:
        return None, None, conflict
    return _best_record(pool), basis, None


def _cluster_conflict(records) -> str | None:
    """非空字段之间的冲突才算冲突；编目条目缺日期不投否决票。"""
    courts = sorted({_normalize_court(record.court) for record in records if record.court})
    if len(courts) > 1:
        return f"同案号候选的审理法院不一致（{'、'.join(courts)}），可能对应不同文书"
    dates = sorted({record.last_instance_date for record in records if record.last_instance_date})
    if len(dates) > 1:
        return f"同案号候选的裁判日期不一致（{'、'.join(dates)}），可能对应同一案件的不同文书"
    return None


def _best_record(records):
    return max(
        records,
        key=lambda record: (
            bool(record.holding),
            bool(record.fulltext),
            bool(record.url),
            bool(record.gid),
        ),
    )


def _safe_natural_person_pair(parties: tuple[str, str]) -> bool:
    organization_tokens = ("公司", "集团", "法院", "委员会", "事务所", "中心", "协会")
    return all(
        1 < len(party) <= 6 and not any(token in party for token in organization_tokens)
        for party in parties
    )


def _verified(record, cited_name, basis, traces):
    traces[-1].status = CaseLookupStatus.VERIFIED
    traces[-1].source_url = record.url
    traces[-1].message = "案例身份匹配通过"
    traces[-1].metadata["match_basis"] = basis
    return (
        CaseLookupStatus.VERIFIED,
        _case_record_evidence(record, cited_name),
        "",
        traces,
    )


def _same_case(reference: PkulawCaseRecord, records: list[PkulawCaseRecord]):
    if reference.case_number:
        number = _normalize_case_number(reference.case_number)
        matches = [record for record in records if _normalize_case_number(record.case_number) == number]
    else:
        title = _normalize_case_name(reference.title)
        matches = [record for record in records if _normalize_case_name(record.title) == title]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        return None
    resolved, _, _ = _resolve_duplicate_number_records(matches, None)
    return resolved


def _cross_route_match(left, right):
    left_by_number = {_normalize_case_number(record.case_number): record for record in left if record.case_number}
    overlaps = {
        number: record for number, record in left_by_number.items()
        if any(_normalize_case_number(item.case_number) == number for item in right)
    }
    return next(iter(overlaps.values())) if len(overlaps) == 1 else None


def _unique_records(records):
    result = []
    seen = set()
    for record in records:
        # 同一案号可能被供应商返回为两条不同标题/链接的记录。不能按案号
        # 折叠，否则人工确认的真正歧义会被前端隐藏。
        key = record.gid or record.url or (
            f"{_normalize_case_number(record.case_number)}|{_normalize_case_name(record.title)}"
            if record.case_number
            else f"{_normalize_case_name(record.title)}|{_normalize_court(record.court)}"
        )
        if key not in seen:
            seen.add(key)
            result.append(record)
    return result


def _candidate_metadata(record):
    return {
        "title": record.title,
        "case_number": record.case_number,
        "court": record.court,
        "last_instance_date": record.last_instance_date,
        "url": record.url,
    }


def _case_identity_revision(claim_text, ref, evidence, rationale):
    replacements = []
    if ref.case_number and _normalize_case_number(ref.case_number) != _normalize_case_number(evidence.case_number):
        replacements.append((ref.case_number, evidence.case_number))
    if ref.case_name and not _equivalent_case_name(ref.case_name, evidence.title):
        replacements.append((ref.case_name, evidence.title))
    if ref.court and not _same_court(ref.court, evidence.court):
        replacements.append((ref.court, evidence.court))
    revised = claim_text
    changed = False
    for original, target in replacements:
        if not original or not target or original == target:
            continue
        if revised.count(original) != 1:
            return None
        revised = revised.replace(original, target, 1)
        changed = True
    if not changed or revised == claim_text:
        return None
    return RevisionProposal(
        strategy="replace_exact_text",
        original_text=claim_text,
        revised_text=revised,
        rationale=rationale,
        machine_applicable=True,
        preconditions=[
            "original_text_unique",
            "document_unchanged",
            "candidate_deterministically_verified",
        ],
    )


def _case_record_evidence(record, cited_name):
    return CaseEvidence(
        matched_text=cited_name or record.title,
        case_number=record.case_number,
        gid=record.gid,
        court=record.court,
        title=record.title,
        last_instance_date=record.last_instance_date,
        url=record.url,
        holding=record.holding,
    )


def _check_holding(claim, evidence, semantic_checker):
    if claim.claim_type != ClaimType.CASE_HOLDING_PARAPHRASE:
        return None
    if evidence is None or not evidence.holding:
        return CaseHoldingCheck(
            execution_status=ExecutionStatus.SKIPPED,
            skipped_reason="holding_unavailable",
            notes="案例身份已经确认，但北大法宝 MCP 中未收录该案裁判说理，引用表述需人工核对原文书。",
        )
    compare = getattr(semantic_checker, "compare_holding", None)
    if not callable(compare):
        return CaseHoldingCheck(
            execution_status=ExecutionStatus.SKIPPED,
            skipped_reason="semantic_disabled",
        )
    try:
        with measure("comparison.llm"):
            return compare(claim.text, evidence.holding, evidence.title)
    except Exception as exc:
        from ..verification.semantic import SemanticCheckError
        if not isinstance(exc, SemanticCheckError):
            raise
        return CaseHoldingCheck(
            execution_status=ExecutionStatus.LLM_ERROR,
            notes=str(exc),
            error_code=getattr(exc, "error_code", "semantic_error"),
        )


def _case_error_status(exc: PkulawMcpError) -> CaseLookupStatus:
    return CaseLookupStatus.SOURCE_NOT_CONFIGURED if isinstance(exc, PkulawNotConfiguredError) else CaseLookupStatus.SOURCE_ERROR


__all__ = ["verify_case_claims"]
