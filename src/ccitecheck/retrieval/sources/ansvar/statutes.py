"""Ansvar 多法域法规溯源源适配器。

复用现有 LookupRequest → LookupResult → ArticleEvidence 契约。查询过程：
确定法域 → 检索候选 → 核对法律身份 → 按返回的精确引用获取条文 → 交给
现有核查器。不把搜索第一条或摘要当作权威条文；条文必须经 get_provision
取回。额度超限（cap_exceeded）、套餐不可用、接口故障均归为 SOURCE_ERROR，
绝不塌缩成"法律不存在"或"核查通过"。
"""

from __future__ import annotations

import re
import threading
from dataclasses import replace
from typing import Optional

from ....domain.evidence import (
    ArticleEvidence,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from ....domain.legal_numbers import chinese_number_to_int
from ..base import LookupRequest, LookupResult
from .client import (
    AnsvarMcpClient,
    AnsvarMcpError,
    AnsvarNotConfiguredError,
    AnsvarPackageUnavailableError,
    AnsvarQuotaExceededError,
    AnsvarRecord,
)
from .cache import CachedAnsvarClient, cache_enabled

SOURCE_NAME = "Ansvar Gateway"
# 常见涉外法规中文名 → (官方外文检索名, 法域代码)。命中别名提高召回。
FOREIGN_LAW_ALIASES: dict[str, tuple[str, str]] = {
    "知识产权法典": ("Code de la propriété intellectuelle", "FR"),
    "人工智能发展及建立信任基础基本法": (
        "인공지능 발전과 신뢰 기반 조성 등에 관한 기본법",
        "KR",
    ),
}


class AnsvarSource:
    """按 StatuteSource 协议实现的 Ansvar 多法域查询。"""

    def __init__(self, client: Optional[AnsvarMcpClient] = None):
        self._client = client
        self._client_lock = threading.Lock()

    def lookup(self, request: LookupRequest) -> LookupResult:
        client = self._client
        if client is None:
            try:
                with self._client_lock:
                    if self._client is None:
                        real_client = AnsvarMcpClient()
                        self._client = (
                            CachedAnsvarClient(real_client)
                            if cache_enabled() else real_client
                        )
                    client = self._client
            except AnsvarNotConfiguredError:
                return self._error_result(
                    request, LookupStatus.SOURCE_NOT_CONFIGURED, "Ansvar 网关未配置"
                )

        query, override_jur = self._build_query(request.law_title)
        jurisdiction = override_jur or request.jurisdiction or ""
        gateway_jurisdiction = jurisdiction.split("-", 1)[0]
        article_locator = _normalize_article_for_lookup(request.article_no)
        article = None
        try:
            # 涉外引文通常同时带法名和条号，先走 get_provision 精确解析。
            if article_locator:
                article = client.get_article_text(
                    query,
                    article_locator,
                    jurisdiction=gateway_jurisdiction,
                )
            if article is not None:
                match = AnsvarRecord(
                    title=article.get("title") or query,
                    identifier=article.get("resolved_canonical_ref") or query,
                    url=article.get("url") or "",
                    jurisdiction=gateway_jurisdiction,
                    in_force=article.get("in_force"),
                    version_label=article.get("version_label") or "",
                    publisher=article.get("publisher") or "",
                    license=article.get("license") or "",
                    citation=article.get("citation") or {},
                )
            else:
                records = client.search_law(query, jurisdiction=gateway_jurisdiction)
                match = _pick_match(records, gateway_jurisdiction)
                if match is not None and article_locator and (
                    match.identifier or match.lookup_arguments
                ):
                    article = client.get_article_text(
                        match.identifier,
                        article_locator,
                        jurisdiction=gateway_jurisdiction,
                        lookup_arguments=match.lookup_arguments,
                    )
        except AnsvarQuotaExceededError as exc:
            # 额度超限 ≠ 法律不存在；归 SOURCE_ERROR 保留人工核查
            return self._error_result(
                request, LookupStatus.SOURCE_ERROR,
                f"Ansvar 额度超限，无法完成核查：{exc}",
                metadata={"error_type": "quota_exceeded"},
            )
        except AnsvarPackageUnavailableError as exc:
            return self._error_result(
                request, LookupStatus.SOURCE_ERROR,
                f"Ansvar 套餐不可用，无法完成核查：{exc}",
                metadata={"error_type": "package_unavailable"},
            )
        except AnsvarMcpError as exc:
            return self._error_result(request, LookupStatus.SOURCE_ERROR, str(exc))

        if match is None:
            trace = SourceTrace(
                tier=SourceTier.ANSVAR,
                source_name=SOURCE_NAME,
                status=LookupStatus.LAW_NOT_FOUND,
                message=(
                    f"Ansvar 未检索到《{request.law_title}》"
                    f"（查询词：{query}，法域：{jurisdiction or '未指定'}）"
                ),
                metadata={"query": query, "jurisdiction": jurisdiction},
            )
            return LookupResult(trace.status, None, trace)

        # 按返回的精确引用取回条文，不把摘要当权威原文
        article_text = None
        article_meta: dict = {}
        if article is not None:
            article_text = _strip_heading(article["text"], article_locator)
            article_title = article.get("title") or ""
            match = replace(
                match,
                title=(match.title if article_title == match.identifier else article_title)
                or match.title,
                identifier=article.get("resolved_canonical_ref") or match.identifier,
                url=article.get("url") or match.url,
                in_force=(
                    article["in_force"]
                    if isinstance(article.get("in_force"), bool) else match.in_force
                ),
                version_label=article.get("version_label") or match.version_label,
                publisher=article.get("publisher") or match.publisher,
                license=article.get("license") or match.license,
                citation=article.get("citation") or match.citation,
            )
            article_meta = {
                "publisher": match.publisher,
                "license": match.license,
                "citation": match.citation,
                "last_verified": article.get("last_verified") or "",
                "resolved_canonical_ref": article.get("resolved_canonical_ref") or "",
                "resolution_method": article.get("resolution_method") or "",
                "version_date": article.get("version_date") or "",
                # 辅助译文：语义比对辅助，不替代原文作为直接引用依据
                "translation": article.get("translation") or "",
            }

        version_status = _version_status(match.in_force)
        if article_text:
            status = LookupStatus.ARTICLE_FOUND
        elif article_locator:
            status = LookupStatus.LAW_FOUND_ARTICLE_MISSING
        else:
            status = LookupStatus.RELEVANT_ARTICLES_FOUND
        if article_text:
            message = f"Ansvar 已取得条文原文：{match.title}"
        elif article_locator:
            message = f"Ansvar 已确认法规存在，但未取得所引条文：{match.title}"
        else:
            message = f"Ansvar 已确认该法规存在：{match.title}"
        trace = SourceTrace(
            tier=SourceTier.ANSVAR,
            source_name=SOURCE_NAME,
            source_url=match.url or None,
            status=status,
            message=message,
            metadata={
                "query": query,
                "jurisdiction": match.jurisdiction or jurisdiction,
                "law_identifier": match.identifier,
                "version_label": match.version_label,
            },
        )
        evidence = ArticleEvidence(
            law_title=match.title,
            source_type="foreign_legal_act",
            article_no=request.article_no,
            article_text=article_text,
            version_label=match.version_label or None,
            version_status=version_status,
            source_metadata={
                "law_identifier": match.identifier,
                "jurisdiction": match.jurisdiction or jurisdiction,
                "cited_title": request.law_title,
                "publisher": article_meta.get("publisher") or match.publisher,
                "license": article_meta.get("license") or match.license,
                **({"citation": match.citation} if match.citation else {}),
                **{
                    key: value
                    for key in (
                        "last_verified", "resolved_canonical_ref",
                        "resolution_method", "version_date", "translation",
                    )
                    if (value := article_meta.get(key) or match.citation.get(key))
                },
            },
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)

    @staticmethod
    def _build_query(law_title: str) -> tuple[str, str]:
        alias = FOREIGN_LAW_ALIASES.get(law_title.strip())
        if alias:
            return alias
        return law_title, ""

    @staticmethod
    def _error_result(
        request: LookupRequest, status: LookupStatus, message: str,
        *, metadata: dict | None = None,
    ) -> LookupResult:
        trace = SourceTrace(
            tier=SourceTier.ANSVAR,
            source_name=SOURCE_NAME,
            status=status,
            message=message,
            metadata=metadata or {},
        )
        return LookupResult(status, None, trace)


_CN_ARTICLE_PATTERN = re.compile(r"^第([一二三四五六七八九十百千零两0-9]+)条$")


def _normalize_article_for_lookup(article_no: Optional[str]) -> str:
    """把中文「第十三条」转为阿拉伯数字串；外文条号（§ 4 / Art. 13）原样透传。"""
    if not article_no:
        return ""
    stripped = article_no.strip()
    cn_match = _CN_ARTICLE_PATTERN.fullmatch(stripped)
    if cn_match:
        return str(chinese_number_to_int(cn_match.group(1)))
    return stripped


def _version_status(in_force: Optional[bool]) -> Optional[str]:
    if in_force is True:
        return "现行有效"
    if in_force is False:
        return "已失效"
    return None


def _strip_heading(text: str, article_locator: str) -> str:
    """去掉正文开头重复的条号标题行。"""
    num = re.sub(r"[^\d]", "", article_locator)
    if num:
        text = re.sub(
            rf"^\s*(?:Article|Art\.?|§|Section|Sec\.?|第)\s*{num}\s*[条.。]?\s*\n+",
            "", text, count=1, flags=re.IGNORECASE,
        )
    return text


def _pick_match(
    records: list[AnsvarRecord], jurisdiction: str
) -> Optional[AnsvarRecord]:
    """优先返回法域匹配的候选；明确冲突时不跨法域取结果。"""
    if not records:
        return None
    if jurisdiction:
        for record in records:
            if record.jurisdiction and record.jurisdiction.upper().split("-", 1)[0] == jurisdiction.upper():
                return record
        unscoped = next((record for record in records if not record.jurisdiction), None)
        return unscoped
    return records[0]


__all__ = [
    "FOREIGN_LAW_ALIASES",
    "AnsvarSource",
    "SOURCE_NAME",
]
