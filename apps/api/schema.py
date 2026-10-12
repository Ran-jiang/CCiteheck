"""Microsoft Word 加载项的 HTTP 请求与响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ccitecheck.domain.result import FrontendVerificationDocument
from ccitecheck.output.summary import VerificationSummary


class DocumentCheckRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    docx_base64: str = Field(min_length=1)
    semantic_check: bool = True
    model: str | None = Field(default=None, description="语义核查模型标识，见 /api/models")
    include_statutes: bool = True
    include_cases: bool = True


class SelectionSourceBlock(BaseModel):
    """选区中一行文本在当前 Word 文档里的真实起点。"""

    block_id: str = Field(min_length=1)
    char_start: int = Field(ge=0)


class SelectionCheckRequest(BaseModel):
    """核查用户在 Word 中选中的文本片段。"""

    file_name: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=200_000)
    semantic_check: bool = True
    include_statutes: bool = True
    include_cases: bool = True
    model: str | None = Field(default=None, description="语义核查模型标识，见 /api/models")
    source_blocks: list[SelectionSourceBlock] = Field(default_factory=list)
    debug_docx_base64: str | None = None


CheckSummary = VerificationSummary


class DocumentCheckResponse(BaseModel):
    file_name: str
    document_key: str
    semantic_check: bool
    summary: CheckSummary
    verification: FrontendVerificationDocument
    debug_run_id: str | None = None


class DebugEventRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    event: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)
