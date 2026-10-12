"""Word 文档解析、引用识别和核查判定的应用层用例。"""

from __future__ import annotations

from pathlib import Path

from ..domain.citation import ClaimDocument
from ..domain.citation_integrity import CitationIntegrityError
from ..domain.document import ParsedDocument
from ..parsing import build_chunks, parse_docx, validate_parsed_document
from ..domain.result import FrontendVerificationDocument
from ..verification import QwenSemanticChecker, SemanticCheckError
from ..recognition import extract_claims
from ..orchestration.scheduler import SchedulerContext, VerificationScheduler


class DocumentPipelineError(RuntimeError):
    """应用层可直接反馈给调用方的文档核查错误。"""


def validate_shared_document(parsed_document: ParsedDocument) -> ParsedDocument:
    """校验任意输入适配器生成的平台无关文档结构。"""
    violations = validate_parsed_document(parsed_document)
    if violations:
        details = "\n".join(
            f"{index}. {item}" for index, item in enumerate(violations, 1)
        )
        raise DocumentPipelineError(f"文档结构校验失败：\n{details}")
    return parsed_document


def parse_and_validate_document(input_path: str | Path) -> ParsedDocument:
    """解析 DOCX 文件并校验解析产物的不变量。"""
    try:
        parsed_document = build_chunks(parse_docx(str(input_path)))
    except Exception as exc:
        raise DocumentPipelineError(f"文档解析失败：{exc}") from exc

    return validate_shared_document(parsed_document)


def extract_document_claims(
    parsed_document: ParsedDocument,
    include_statutes: bool = True,
    include_cases: bool = True,
) -> ClaimDocument:
    """从解析后的文档中识别指定类型的法律引用。"""
    try:
        return extract_claims(parsed_document, include_statutes, include_cases)
    except ValueError as exc:
        raise DocumentPipelineError(str(exc)) from exc


def verify_document_claims(
    claim_document: ClaimDocument,
    law_db: str | Path,
    semantic_check: bool,
    qwen_model: str | None = None,
    include_statutes: bool = True,
    include_cases: bool = True,
) -> FrontendVerificationDocument:
    """溯源并判定已识别的引用，返回统一前端结果模型。"""
    semantic_checker = None
    if semantic_check and include_statutes:
        try:
            semantic_checker = QwenSemanticChecker.from_env(qwen_model)
        except SemanticCheckError as exc:
            raise DocumentPipelineError(str(exc)) from exc

    try:
        return VerificationScheduler(SchedulerContext(
            law_db=law_db,
            semantic_checker=semantic_checker,
        )).verify_document(
            claim_document,
            include_statutes=include_statutes,
            include_cases=include_cases,
        )
    except CitationIntegrityError as exc:
        raise DocumentPipelineError(str(exc)) from exc
