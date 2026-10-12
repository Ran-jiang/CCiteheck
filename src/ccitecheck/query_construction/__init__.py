"""查询假设构造层。"""

from .service import (
    DocumentQueryContext,
    QueryResources,
    build_document_context,
    build_initial_hypothesis,
    rebuild_hypothesis,
    resolve_identity_candidates,
)

__all__ = [
    "DocumentQueryContext",
    "QueryResources",
    "build_document_context",
    "build_initial_hypothesis",
    "rebuild_hypothesis",
    "resolve_identity_candidates",
]
