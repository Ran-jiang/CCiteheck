"""北大法宝工具响应到领域模型的解析。"""

from __future__ import annotations

import re
from typing import Any, Optional

from ....infrastructure.database import normalize_article_key
from .models import (
    PkulawArticle,
    PkulawCaseRecord,
    PkulawLawRecord,
    PkulawMcpError,
    PkulawNotFoundError,
    PkulawRecognizedLaw,
)
from .urls import usable_mcp_url


def parse_exact_article(data: Any, requested_article_no: str) -> PkulawArticle:
    item = response_data(data)
    if isinstance(item, list):
        item = item[0] if len(item) == 1 else None
    if item is None or (isinstance(item, str) and "未找到" in item):
        raise PkulawNotFoundError("未找到数据")
    if not isinstance(item, dict):
        raise PkulawMcpError("Unexpected get_article response shape")
    record = flatten_metadata(item)
    title = first_value(record, "Title", "title")
    text = first_value(record, "Article", "article", "FullText", "fulltext")
    if not title and not text:
        raise PkulawNotFoundError("未找到数据")
    if title and not text:
        raise PkulawNotFoundError("未找到数据")
    if not title or not text:
        raise PkulawMcpError("Pkulaw response is missing title or article text")
    returned_article_no = article_heading(str(text))
    if (
        returned_article_no
        and normalize_article_key(returned_article_no)
        != normalize_article_key(requested_article_no)
    ):
        raise PkulawMcpError(
            f"Pkulaw returned {returned_article_no} for {requested_article_no}"
        )
    return article_from_record(parse_law_record(record), requested_article_no, str(text))


def parse_case_records(data: Any) -> list[PkulawCaseRecord]:
    return [
        parsed
        for item in response_records(data)
        if isinstance(item, dict)
        if (parsed := parse_case_record(item)) is not None
    ]


_SEMANTIC_CASE_HEADER = re.compile(
    r"(?m)^(?P<number>\d+)\.\s+\[[^\]]+\]\s+"
    r"(?P<title>.+?)[ \t]*\|[ \t]*(?P<case_number>[^\n]*)$"
)


def parse_semantic_case_text(text: str) -> list[PkulawCaseRecord]:
    """解析北大法宝 search_case 当前返回的可读文本候选。"""
    headers = list(_SEMANTIC_CASE_HEADER.finditer(text))
    records: list[PkulawCaseRecord] = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[header.end():end].strip()

        def field(label: str) -> str:
            match = re.search(rf"(?:^|\|\s*){re.escape(label)}：([^|\n]+)", block, re.MULTILINE)
            return match.group(1).strip() if match else ""

        url_match = re.search(r"(?m)^\s*链接：(https?://\S+)", block)
        fulltext = re.sub(r"(?m)^\s*链接：https?://\S+\s*$", "", block).strip()
        records.append(PkulawCaseRecord(
            title=header.group("title").strip(),
            case_number=header.group("case_number").strip(),
            court=field("审理法院"),
            last_instance_date=field("审结日期") or None,
            url=url_match.group(1) if url_match else None,
            fulltext=fulltext or None,
            holding=fulltext or None,
        ))
    return records


def parse_case_record(item: dict[str, Any]) -> PkulawCaseRecord | None:
    record = flatten_metadata(item)
    title = first_value(record, "Title", "title")
    case_number = first_value(record, "CaseNO", "CaseFlag", "case_number")
    if not title and not case_number:
        return None
    return PkulawCaseRecord(
        title=str(title or case_number),
        case_number=str(case_number or ""),
        gid=str(first_value(record, "Gid", "gid") or ""),
        court=str(first_value(record, "Court", "court", "courthouse_name") or ""),
        last_instance_date=optional_text(
            first_value(record, "LastInstanceDate", "lastInstanceDate")
        ),
        url=usable_mcp_url(optional_text(first_value(record, "Url", "url"))),
        fulltext=optional_text(first_value(record, "FullText", "fulltext")),
        holding=optional_text(first_value(record, "CaseGist", "Identified", "identified")),
    )


def parse_law_records(data: Any) -> list[PkulawLawRecord]:
    return [
        parse_law_record(record)
        for record in response_records(data)
        if isinstance(record, dict)
    ]


def parse_article_records(data: Any) -> list[PkulawArticle]:
    articles: list[PkulawArticle] = []
    for raw in response_records(data):
        if not isinstance(raw, dict):
            continue
        record = flatten_metadata(raw)
        title = first_value(record, "Title", "title")
        article_text = first_value(
            record, "FullText", "fulltext", "ArticleText", "article_text", "article"
        )
        if not title or not article_text:
            continue
        base = parse_law_record(record)
        article_no = first_value(record, "ArticleNO", "article_no") or article_heading(
            str(article_text)
        )
        articles.append(
            article_from_record(base, str(article_no or ""), str(article_text))
        )
    return articles


def parse_recognized_laws(data: Any) -> list[PkulawRecognizedLaw]:
    laws: list[PkulawRecognizedLaw] = []
    for item in response_records(data):
        if not isinstance(item, dict):
            continue
        canonical = first_value(item, "original", "Original")
        fulltext = first_value(item, "fulltext", "Fulltext", "FullText")
        if not canonical or not fulltext:
            continue
        laws.append(PkulawRecognizedLaw(
            mentioned_title=str(first_value(item, "text", "Text") or canonical),
            canonical_title=str(canonical),
            fulltext=str(fulltext),
            url=usable_mcp_url(optional_text(first_value(item, "source", "Source"))),
        ))
    return laws


def parse_law_record(record: dict[str, Any]) -> PkulawLawRecord:
    return PkulawLawRecord(
        title=str(first_value(record, "Title", "title") or ""),
        url=usable_mcp_url(optional_text(first_value(record, "Url", "url"))),
        category=as_list(first_value(record, "Category", "category")),
        document_no=optional_text(
            first_value(record, "DocumentNO", "document_no", "doc_no")
        ),
        issue_department=as_list(
            first_value(record, "IssueDepartment", "issue_department")
        ),
        issue_date=optional_text(first_value(record, "IssueDate", "issue_date")),
        implement_date=optional_text(
            first_value(
                record, "ImplementDate", "implement_date", "implementation_date"
            )
        ),
        timeliness=as_list(first_value(record, "TimelinessDic", "timeliness")),
        effectiveness=as_list(first_value(record, "EffectivenessDic", "effectiveness")),
    )


def response_data(data: Any) -> Any:
    if isinstance(data, dict):
        message = data.get("Message")
        if message not in (None, "成功", "success"):
            text = str(message)
            if "未找到" in text or "无数据" in text:
                raise PkulawNotFoundError(text)
            raise PkulawMcpError(text)
        if "Data" in data:
            return data["Data"]
        if "data" in data:
            return data["data"]
    return data


def response_records(data: Any) -> list[Any]:
    value = response_data(data)
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("items", "records", "results"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    if value in (None, {}):
        return []
    raise PkulawMcpError("Unexpected Pkulaw list response shape")


def article_from_record(
    base: PkulawLawRecord, article_no: str, article_text: str
) -> PkulawArticle:
    return PkulawArticle(
        title=base.title,
        url=base.url,
        category=base.category,
        document_no=base.document_no,
        issue_department=base.issue_department,
        issue_date=base.issue_date,
        implement_date=base.implement_date,
        timeliness=base.timeliness,
        effectiveness=base.effectiveness,
        article_no=article_no,
        article_text=strip_article_heading(article_text),
    )


_ARTICLE_HEADING = re.compile(
    r"^\s*(?P<number>第[〇零一二三四五六七八九十百千万两0-9]+条"
    r"(?:之[〇零一二三四五六七八九十百千万两0-9]+)?)[\s　]*"
)


def article_heading(text: str) -> str:
    match = _ARTICLE_HEADING.match(text)
    return match.group("number") if match else ""


def strip_article_heading(text: str) -> str:
    return _ARTICLE_HEADING.sub("", text, count=1)


def flatten_metadata(record: dict[str, Any]) -> dict[str, Any]:
    metadata = record.get("metadata")
    return {**record, **metadata} if isinstance(metadata, dict) else record


def first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def optional_text(value: Any) -> Optional[str]:
    return str(value) if value not in (None, "") else None


def optional_int(value: Any, default: int) -> int:
    return default if value in (None, "") else int(value)


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    return [str(value)]


def compact_case_number(value: str) -> str:
    table = str.maketrans({"（": "(", "）": ")", "〔": "(", "〕": ")"})
    return "".join(str(value).translate(table).split())


__all__ = [
    "parse_article_records",
    "parse_case_records",
    "parse_exact_article",
    "parse_law_records",
    "parse_recognized_laws",
    "parse_semantic_case_text",
]
