"""EUR-Lex 法规溯源源适配器（jurisdiction=EU 专用链路）。

确认法规在 EUR-Lex 存在并取回官方英文名、CELEX 号、链接及可用条文。
英文条文由法律适用核查统一完成跨语言比对；时效字段沿用中文约定
（现行有效/已失效），使确定性废止规则直接生效。
"""

from __future__ import annotations

import re
from typing import Optional

from ....domain.evidence import (
    ArticleEvidence,
    ArticleExcerpt,
    LookupStatus,
    SourceTier,
    SourceTrace,
)
from ....domain.legal_numbers import chinese_number_to_int
from ..base import LookupRequest, LookupResult
from .client import (
    EurLexMcpClient,
    EurLexMcpError,
    EurLexNotConfiguredError,
    EurLexRecord,
)

SOURCE_NAME = "EUR-Lex MCP"

# 常见欧盟法规的中文名 → 官方英文检索词与 CELEX 号。
# 命中别名时用英文官方名检索，显著提高召回；未命中时用原名检索。
EU_LAW_ALIASES: dict[str, tuple[str, str]] = {
    "通用数据保护条例": ("General Data Protection Regulation 2016/679", "32016R0679"),
    "一般数据保护条例": ("General Data Protection Regulation 2016/679", "32016R0679"),
    "人工智能法案": ("Artificial Intelligence Act 2024/1689", "32024R1689"),
    "人工智能法": ("Artificial Intelligence Act 2024/1689", "32024R1689"),
    "EU AI Act": ("Artificial Intelligence Act 2024/1689", "32024R1689"),
    "数字市场法": ("Digital Markets Act 2022/1925", "32022R1925"),
    "数字服务法": ("Digital Services Act 2022/2065", "32022R2065"),
    "数据法案": ("Data Act 2023/2854", "32023R2854"),
}

class EurLexSource:
    """按 StatuteSource 协议实现的 EUR-Lex 查询。"""

    def __init__(self, client: Optional[EurLexMcpClient] = None):
        self._client = client

    def lookup(self, request: LookupRequest) -> LookupResult:
        try:
            client = self._client or EurLexMcpClient()
        except EurLexNotConfiguredError as exc:
            return self._error_result(
                request, LookupStatus.SOURCE_NOT_CONFIGURED, str(exc)
            )

        query, celex = self._build_query(request.law_title)
        try:
            records = client.search_law(query, celex=celex)
        except EurLexNotConfiguredError as exc:
            return self._error_result(
                request, LookupStatus.SOURCE_NOT_CONFIGURED, str(exc)
            )
        except EurLexMcpError as exc:
            return self._error_result(request, LookupStatus.SOURCE_ERROR, str(exc))

        match = _pick_match(records, celex)
        if match is None:
            trace = SourceTrace(
                tier=SourceTier.EURLEX,
                source_name=SOURCE_NAME,
                status=LookupStatus.LAW_NOT_FOUND,
                message=f"EUR-Lex 未检索到《{request.law_title}》（查询词：{query}）",
                metadata={"query": query},
            )
            return LookupResult(trace.status, None, trace)

        # 文书引了具体条号时，取回该条英文原文，供展示与适用侧跨语言比对
        article_text = None
        article_number = _article_number(request.article_no)
        in_force = match.in_force
        if article_number is not None and match.celex:
            article = self._fetch_article(client, match.celex, article_number)
            if article is not None:
                article_text = _strip_article_heading(
                    article["text"], article_number
                )
                if article["in_force"] is not None:
                    in_force = article["in_force"]

        version_status = None
        if in_force is True:
            version_status = "现行有效"
        elif in_force is False:
            version_status = "已失效"
        status = (
            LookupStatus.ARTICLE_FOUND
            if article_text
            else LookupStatus.RELEVANT_ARTICLES_FOUND
        )
        message = (
            f"EUR-Lex 已取得 Article {article_number} 原文：{match.title}"
            if article_text
            else f"EUR-Lex 已确认该欧盟法规存在：{match.title}"
        )
        trace = SourceTrace(
            tier=SourceTier.EURLEX,
            source_name=SOURCE_NAME,
            source_url=match.url or None,
            status=status,
            message=message,
            metadata={"query": query, "celex": match.celex},
        )
        evidence = ArticleEvidence(
            law_title=match.title,
            source_type="eu_legal_act",
            # 展示层用欧盟体例的条号（Article N），引用行仍保留中文条号
            article_no=(
                f"Article {article_number}" if article_text else request.article_no
            ),
            article_text=article_text or match.snippet or None,
            version_status=version_status,
            source_metadata={"celex": match.celex, "cited_title": request.law_title},
            data_source=trace,
        )
        return LookupResult(trace.status, evidence, trace)

    @staticmethod
    def _fetch_article(
        client: EurLexMcpClient, celex: str, article_number: int
    ) -> Optional[dict]:
        """条文取回失败不影响存在性结论，静默降级。"""
        fetch = getattr(client, "get_article_text", None)
        if not callable(fetch):
            return None
        try:
            return fetch(celex, article_number)
        except EurLexMcpError:
            return None

    @staticmethod
    def _build_query(law_title: str) -> tuple[str, str]:
        alias = EU_LAW_ALIASES.get(law_title.strip())
        if alias:
            return alias
        return law_title, ""

    @staticmethod
    def _error_result(
        request: LookupRequest, status: LookupStatus, message: str
    ) -> LookupResult:
        trace = SourceTrace(
            tier=SourceTier.EURLEX,
            source_name=SOURCE_NAME,
            status=status,
            message=message,
        )
        return LookupResult(status, None, trace)


_ARTICLE_NUM_PATTERN = re.compile(r"^第([一二三四五六七八九十百千零两0-9]+)条$")


def _article_number(article_no: Optional[str]) -> Optional[int]:
    """「第十七条」→ 17；带「之一」等后缀的不做映射（欧盟法无对应体例）。"""
    if not article_no:
        return None
    match = _ARTICLE_NUM_PATTERN.fullmatch(article_no.strip())
    if not match:
        return None
    return chinese_number_to_int(match.group(1))


article_number_from_citation = _article_number


def _strip_article_heading(text: str, article_number: int) -> str:
    """去掉正文开头重复的「Article N」标题行（展示层已有条号标题）。"""
    return re.sub(rf"^\s*Article\s+{article_number}\s*\n+", "", text, count=1)


def fetch_article_excerpt(celex: str, article_number: int) -> Optional[ArticleExcerpt]:
    """独立取回一条欧盟条文作为参考条款（用于抓错后补取建议条文）。"""
    try:
        client = EurLexMcpClient()
        article = client.get_article_text(celex, article_number)
    except EurLexMcpError:
        return None
    if article is None:
        return None
    return ArticleExcerpt(
        article_no=f"Article {article_number}",
        article_text=_strip_article_heading(article["text"], article_number),
        relevance_score=1.0,
    )


def _pick_match(records: list[EurLexRecord], celex: str) -> Optional[EurLexRecord]:
    if not records:
        return None
    if celex:
        for record in records:
            if record.celex and record.celex.lstrip("0") == celex.lstrip("0"):
                return record
    return records[0]


__all__ = [
    "EU_LAW_ALIASES",
    "EurLexSource",
    "SOURCE_NAME",
    "article_number_from_citation",
    "fetch_article_excerpt",
]
