"""根据法规溯源证据生成确定性判定。"""

from __future__ import annotations

import difflib
import re
from typing import Protocol

from ...domain.evidence import ArticleEvidence, LookupStatus, SourceTier, SourceTrace
from ...domain.law_titles import cn_title_shape_key
from ...domain.statute_results import (
    StatuteErrorCode,
    StatuteFinding,
    StatuteLocator,
    StatuteVersion,
)
from ...domain.revisions import replacement_revision
from ...infrastructure.database import normalize_title, strip_version_annotation


class LookupResult(Protocol):
    status: LookupStatus
    evidence: ArticleEvidence | None

_REPEALED_PATTERN = re.compile(r"废止或失效|已(?:被)?废止|已失效|^(?:废止|失效)$")
_AMENDED_PATTERN = re.compile(r"已被修改|部分(?:废止|失效)")
_IMPLEMENT_DATE_PATTERN = re.compile(
    r"(?P<date>(?:19|20)\d{2}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?)"
    r"(?:起)?(?:施行|实施|生效)"
)
_GB_STANDARD_PATTERN = re.compile(r"GB\s*/?\s*[TZ]?\s*\d{3,6}")


def classify_not_verifiable(law_title: str) -> str | None:
    if "征求意见稿" in law_title:
        return "征求意见稿尚未生效，不属于可核验的现行法源，请人工确认引用意图"
    if _GB_STANDARD_PATTERN.search(law_title):
        return "国家/行业标准不在法规库核验范围内，请以标准全文出版物为准"
    if "专项通知" in law_title:
        return "专项通知不按法条编号核验，请以发布机关原文件及适用期限为准"
    return None


def assess_statute(
    law_title: str,
    article_no: str | None,
    result: LookupResult,
    attempts: list[SourceTrace],
    known_titles: list[str],
    historical_versions: list[StatuteVersion] | None = None,
    *,
    claim_text: str = "",
) -> list[StatuteFinding]:
    """判定法源存在性、时效和条号定位；不执行语义判断。"""
    repealed = _is_repealed(result)
    if repealed:
        return [_repealed_finding(law_title, result)]

    if _is_amended(result):
        return [_amended_finding(law_title, result)]

    metadata_finding = _metadata_finding(law_title, result, claim_text)
    if metadata_finding is not None:
        return [metadata_finding]

    if result.status in {
        LookupStatus.LAW_FOUND_ARTICLE_MISSING,
        LookupStatus.RELEVANT_ARTICLES_FOUND,
    } and article_no:
        missing = _missing_article_finding(law_title, article_no, result, attempts)
        if historical_versions and missing.risk_level == "HIGH":
            version = historical_versions[0]
            return [StatuteFinding(
                code=StatuteErrorCode.SOURCE_AMENDED,
                risk_level="HIGH",
                summary=f"现行《{strip_version_annotation(law_title)}》不存在{article_no}，但历史版本中存在该条",
                suggestion="法源版本或效力错误，请核对适用时间和现行规定。",
                cited_locator=StatuteLocator(article_no=article_no),
                historical_version=version,
            )]
        return [missing]

    pkulaw = _completed_pkulaw_not_found(attempts)
    if result.status == LookupStatus.LAW_NOT_FOUND and pkulaw is not None:
        cited = strip_version_annotation(law_title)
        # 已取回正确法规该条原文时，直接用其法名并提示对照下方权威原文
        corrected = (
            result.evidence.law_title
            if (
                result.evidence
                and result.evidence.article_text
                and pkulaw.metadata.get("suggested_title")
            )
            else None
        )
        if corrected:
            suggestion = f"法律名称引用错误，应为《{strip_version_annotation(corrected)}》。"
            replacement_title = strip_version_annotation(corrected)
        else:
            candidates = list(pkulaw.metadata.get("candidate_titles", []))
            suggested = suggest_similar_title(law_title, [*known_titles, *candidates])
            replacement_title = suggested
            suggestion = (
                f"北大法宝 MCP 未检索到所引法源，疑似应为《{suggested}》，请核实法规名称。"
                if suggested
                else "北大法宝 MCP 未检索到所引法源，请核对法规名称。"
            )
        return [StatuteFinding(
            code=(
                StatuteErrorCode.LAW_NAME_ERROR
                if corrected else StatuteErrorCode.SOURCE_NOT_FOUND
            ),
            risk_level="HIGH",
            summary=f"北大法宝未检索到《{cited}》",
            suggestion=suggestion,
            revision=(
                replacement_revision(
                    claim_text,
                    f"《{cited}》",
                    f"《{replacement_title}》",
                    f"将错误法名更正为《{replacement_title}》",
                    preconditions=["candidate_returned_by_authoritative_source"],
                )
                if replacement_title else None
            ),
        )]

    # 欧盟法规经 EUR-Lex 检索未命中：同样报法源未检索到，避免编造的欧盟
    # 法规因无 finding 而误判通过。
    if result.status == LookupStatus.LAW_NOT_FOUND and _completed_eurlex_not_found(attempts):
        cited = strip_version_annotation(law_title)
        return [StatuteFinding(
            code=StatuteErrorCode.SOURCE_NOT_FOUND,
            risk_level="HIGH",
            summary=f"EUR-Lex 未检索到《{cited}》",
            suggestion="EUR-Lex MCP 未检索到所引法源，请核对法规名称。",
        )]

    if result.status == LookupStatus.LAW_NOT_FOUND and _completed_ansvar_not_found(attempts):
        cited = strip_version_annotation(law_title)
        return [StatuteFinding(
            code=StatuteErrorCode.SOURCE_NOT_FOUND,
            risk_level="HIGH",
            summary=f"Ansvar 未检索到《{cited}》",
            suggestion="Ansvar Gateway 未检索到所引法源，请核对法规名称和法域。",
        )]

    return []


def _completed_eurlex_not_found(attempts: list[SourceTrace]) -> SourceTrace | None:
    return next((
        trace for trace in attempts
        if trace.tier == SourceTier.EURLEX
        and trace.status == LookupStatus.LAW_NOT_FOUND
    ), None)


def _completed_ansvar_not_found(attempts: list[SourceTrace]) -> SourceTrace | None:
    return next((
        trace for trace in attempts
        if trace.tier == SourceTier.ANSVAR
        and trace.status == LookupStatus.LAW_NOT_FOUND
    ), None)


def _is_repealed(result: LookupResult) -> bool:
    evidence = result.evidence
    if evidence is None:
        return False
    values = (
        evidence.version_status or "",
        evidence.version_label or "",
        str(evidence.source_metadata.get("timeliness", "")),
    )
    return any(_REPEALED_PATTERN.search(value) for value in values)


def _repealed_finding(law_title: str, result: LookupResult) -> StatuteFinding:
    title = strip_version_annotation(law_title)
    evidence = result.evidence
    status = (
        evidence.version_status
        or evidence.version_label
        or "废止或失效"
        if evidence else "废止或失效"
    )
    effective_to = (
        evidence.effective_to or evidence.source_metadata.get("effective_to")
        if evidence else None
    )
    basis = _repeal_basis(law_title, result)
    if basis:
        issue_date = basis.get("issue_date")
        implement_date = basis.get("implement_date")
        detail = "；".join(filter(None, (
            f"决定日期：{issue_date}" if issue_date else None,
            f"自{implement_date}起废止" if implement_date else None,
        )))
        suggestion = (
            f"《{title}》已由《{basis['title']}》决定废止"
            f"{'（' + detail + '）' if detail else ''}。"
        )
    else:
        suggestion = (
            f"《{title}》已于{effective_to}失效，请核对废止依据和现行规定。"
            if effective_to
            else f"《{title}》已废止或失效，请核对废止依据和现行规定。"
        )
    return StatuteFinding(
        code=StatuteErrorCode.SOURCE_REPEALED,
        risk_level="HIGH",
        summary=f"《{title}》的权威效力状态为{status}",
        suggestion=suggestion,
    )


def _repeal_basis(law_title: str, result: LookupResult) -> dict | None:
    trace = getattr(result, "trace", None)
    metadata = trace.metadata if trace else {}
    candidates = list(metadata.get("candidates", []))
    for attempt in metadata.get("route_attempts", []):
        candidates.extend(attempt.get("candidates", []))
    target = _plain_title(law_title)
    return next((
        candidate for candidate in candidates
        if isinstance(candidate, dict)
        and "废止" in str(candidate.get("title", ""))
        and target in _plain_title(str(candidate.get("title", "")))
    ), None)


def _plain_title(value: str) -> str:
    plain = re.sub(
        r"[^0-9A-Za-z\u4e00-\u9fff]", "", strip_version_annotation(value)
    )
    return cn_title_shape_key(plain)


def _is_amended(result: LookupResult) -> bool:
    evidence = result.evidence
    if evidence is None:
        return False
    values = (
        evidence.version_status or "",
        evidence.version_label or "",
        str(evidence.source_metadata.get("timeliness", "")),
    )
    return any(_AMENDED_PATTERN.search(value) for value in values)


def _amended_finding(law_title: str, result: LookupResult) -> StatuteFinding:
    evidence = result.evidence
    current = next((
        route for route in reversed(
            (getattr(result, "trace", None).metadata.get("route_attempts", []))
            if getattr(result, "trace", None) else []
        )
        if route.get("current_title")
    ), {})
    cited = evidence.law_title if evidence else law_title
    metadata = evidence.data_source.metadata if evidence else {}
    latest = current.get("current_title") or metadata.get("current_title")
    effective = (
        current.get("current_implement_date")
        or metadata.get("current_implement_date")
    )
    suffix = f"（自{effective}施行）" if effective else ""
    return StatuteFinding(
        code=StatuteErrorCode.SOURCE_AMENDED,
        risk_level="HIGH",
        summary=f"所引《{cited}》已被修改",
        suggestion=(
            f"所引版本已被修改，最新版为《{latest}》{suffix}，请核对适用时间和现行规定。"
            if latest else "所引版本已被修改，请核对适用时间和现行规定。"
        ),
    )


def _metadata_finding(
    law_title: str, result: LookupResult, claim_text: str
) -> StatuteFinding | None:
    evidence = result.evidence
    if evidence is None or not claim_text:
        return None
    status = "".join((
        evidence.version_status or "",
        evidence.version_label or "",
        str(evidence.source_metadata.get("timeliness", "")),
    ))
    if (
        re.search(r"已(?:被)?废止|已失效|尚未生效", claim_text)
        and "现行有效" in status
    ):
        return StatuteFinding(
            code=StatuteErrorCode.SOURCE_AMENDED,
            risk_level="HIGH",
            summary=f"《{strip_version_annotation(law_title)}》的权威效力状态为现行有效",
            suggestion="原文效力状态错误，应改为现行有效。",
        )
    claimed = _IMPLEMENT_DATE_PATTERN.search(claim_text)
    authoritative = evidence.effective_from or evidence.source_metadata.get("implement_date")
    if claimed and authoritative and _date_key(claimed.group("date")) != _date_key(str(authoritative)):
        return StatuteFinding(
            code=StatuteErrorCode.SOURCE_AMENDED,
            risk_level="HIGH",
            summary=(
                f"原文所述施行日期为{claimed.group('date')}，"
                f"权威记录为{authoritative}"
            ),
            suggestion=f"施行日期错误，应为{authoritative}。",
        )
    if "尚未生效" in status and not (
        claimed
        and authoritative
        and _date_key(claimed.group("date")) == _date_key(str(authoritative))
    ):
        suffix = f"，将自{authoritative}施行" if authoritative else ""
        return StatuteFinding(
            code=StatuteErrorCode.SOURCE_AMENDED,
            risk_level="HIGH",
            summary=f"《{strip_version_annotation(law_title)}》尚未生效{suffix}",
            suggestion="当前不能作为已生效法源适用，请核对适用时间。",
        )
    return None


def _date_key(value: str) -> tuple[int, int, int] | None:
    parts = re.findall(r"\d+", value)
    return tuple(map(int, parts[:3])) if len(parts) >= 3 else None


def _missing_article_finding(
    law_title: str,
    article_no: str,
    result: LookupResult,
    attempts: list[SourceTrace],
) -> StatuteFinding:
    complete = next((trace for trace in attempts
                     if trace.metadata.get("full_text_complete") is True
                     and trace.metadata.get("version_confirmed") is True
                     and trace.metadata.get("completeness_source")
                     and isinstance(trace.metadata.get("article_keys"), list)), None)
    from ...infrastructure.database import normalize_article_key
    if complete and normalize_article_key(article_no) not in {
        normalize_article_key(key) for key in complete.metadata["article_keys"]
    }:
        summary = f"《{strip_version_annotation(law_title)}》该版本中不存在所引条号{article_no}"
        risk = "HIGH"
        suggestion = "请核对条文序号及适用版本。"
    else:
        summary = "未精确命中所引条文，已召回相关条文待核查" if result.evidence and result.evidence.related_articles else "未精确命中所引条文，尚不能确认条号不存在"
        risk = "MEDIUM"
        suggestion = "请结合完整、适用版本的法规原文核实引用位置。"
    return StatuteFinding(
        code=StatuteErrorCode.ARTICLE_NOT_FOUND,
        risk_level=risk,
        summary=summary,
        suggestion=suggestion,
        cited_locator=StatuteLocator(article_no=article_no),
    )


def _completed_pkulaw_not_found(attempts: list[SourceTrace]) -> SourceTrace | None:
    return next((
        trace
        for trace in attempts
        if trace.tier == SourceTier.PKULAW_FALLBACK
        and trace.status == LookupStatus.LAW_NOT_FOUND
        and trace.metadata.get("search_completed") is True
    ), None)


def suggest_similar_title(law_title: str, known_titles: list[str]) -> str | None:
    if not known_titles:
        return None
    target = strip_version_annotation(normalize_title(law_title))
    matches = difflib.get_close_matches(target, known_titles, n=1, cutoff=0.8)
    if matches:
        return matches[0]
    short = target.replace("中华人民共和国", "", 1)
    shorts = [title.replace("中华人民共和国", "", 1) for title in known_titles]
    matches = difflib.get_close_matches(short, shorts, n=1, cutoff=0.8)
    return known_titles[shorts.index(matches[0])] if matches else None


__all__ = ["assess_statute", "classify_not_verifiable", "suggest_similar_title"]
