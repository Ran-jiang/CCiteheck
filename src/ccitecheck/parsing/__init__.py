"""DOCX 解析与文档结构校验。"""

from .chunks import build_chunks
from .docx import parse_docx
from .relations import build_block_relations
from .validators import validate_parsed_document

__all__ = ["build_block_relations", "build_chunks", "parse_docx", "validate_parsed_document"]
