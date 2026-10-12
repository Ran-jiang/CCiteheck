"""Recognition 的不可变原文事实模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .citation import (
    AliasDeclaration,
    Claim,
    ClaimDocument,
    ClaimType,
    NoteContext,
    SourceLocation,
)
from .citation_integrity import legal_source_alias_index


class RawCitationLocator(BaseModel):
    article_raw: str | None = None
    paragraphs_raw: list[str] = Field(default_factory=list)
    items_raw: list[str] = Field(default_factory=list)
    structures_raw: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


class InheritedFrom(BaseModel):
    anchor_id: str | None = None
    source_location: SourceLocation | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


class RawLegalMention(BaseModel):
    mention_id: str
    raw_title: str | None = None
    raw_title_candidate: str | None = None
    raw_time: str | None = None
    jurisdiction: str | None = None
    article_raw: str | None = None
    paragraphs_raw: list[str] = Field(default_factory=list)
    items_raw: list[str] = Field(default_factory=list)
    structures_raw: list[str] = Field(default_factory=list)
    recognition_form: Literal["explicit", "bare", "inherited"]
    mention_span: tuple[int, int] | None = None
    citation_span: tuple[int, int] | None = None
    inherited_from: InheritedFrom | None = None
    role: Literal["direct", "nested", "carry_forward"] = "direct"
    model_config = ConfigDict(extra="forbid", frozen=True)


class RawCaseMention(BaseModel):
    mention_id: str
    raw_case_number: str | None = None
    raw_case_name: str | None = None
    raw_court: str | None = None
    raw_document_type: str | None = None
    mention_span: tuple[int, int] | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimRelation(BaseModel):
    parent_mention_id: str
    child_mention_id: str
    basis: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class RawClaim(BaseModel):
    claim_id: str
    claim_type: ClaimType
    raw_text: str
    context_text: str = ""
    anchor_ids: list[str] = Field(default_factory=list)
    source_locations: list[SourceLocation] = Field(default_factory=list)
    note_context: NoteContext | None = None
    legal_mentions: list[RawLegalMention] = Field(default_factory=list)
    case_mentions: list[RawCaseMention] = Field(default_factory=list)
    alias_declarations: list[AliasDeclaration] = Field(default_factory=list)
    structural_relations: list[ClaimRelation] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


class RawClaimDocument(BaseModel):
    source_claim_doc_id: str
    source_doc_id: str = ""
    document_text: str = ""
    claims: list[RawClaim] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


def from_legacy_claim(claim: Claim) -> RawClaim:
    """把第一阶段兼容 Claim 投影为不含法名解释的 RawClaim。"""
    legal_mentions: list[RawLegalMention] = []
    case_mentions: list[RawCaseMention] = []
    entities = claim.entities
    sources = list(getattr(entities, "legal_sources", []))
    citations = list(getattr(entities, "citations", []))
    source_by_title = legal_source_alias_index(sources)
    cited_source_ids: set[int] = set()
    for citation_index, citation in enumerate(citations, 1):
        source = source_by_title.get(citation.law_title)
        if source is None:
            continue
        cited_source_ids.add(id(source))
        legal_mentions.append(RawLegalMention(
            mention_id=citation.mention_id or f"{claim.claim_id}:law:{citation_index}",
            raw_title=source.title or None,
            raw_title_candidate=source.raw_title_candidate,
            raw_time=source.raw_time,
            jurisdiction=source.jurisdiction,
            article_raw=citation.locator.article,
            paragraphs_raw=(
                [citation.locator.paragraph] if citation.locator.paragraph else []
            ),
            items_raw=[citation.locator.item] if citation.locator.item else [],
            recognition_form=source.recognition.form,
            mention_span=source.recognition.mention_span,
            citation_span=citation.citation_span,
            inherited_from=_inherited_from(source),
            role=citation.role,
        ))
    for source_index, source in enumerate(sources, 1):
        if id(source) in cited_source_ids:
            continue
        articles = source.articles or [None]
        for article_index, article in enumerate(articles, 1):
            legal_mentions.append(RawLegalMention(
                mention_id=f"{claim.claim_id}:law:{source_index}:{article_index}",
                raw_title=source.title or None,
                raw_title_candidate=source.raw_title_candidate,
                raw_time=source.raw_time,
                jurisdiction=source.jurisdiction,
                article_raw=article.article if article else None,
                paragraphs_raw=list(article.paragraphs) if article else [],
                items_raw=list(article.items) if article else [],
                structures_raw=[item.label for item in source.structures],
                recognition_form=source.recognition.form,
                mention_span=source.recognition.mention_span,
                inherited_from=_inherited_from(source),
            ))
    for unresolved_index, mention in enumerate(
        getattr(entities, "unresolved_legal_mentions", []), 1
    ):
        articles = mention.articles or [None]
        for article_index, article in enumerate(articles, 1):
            legal_mentions.append(RawLegalMention(
                mention_id=f"{claim.claim_id}:bare:{unresolved_index}:{article_index}",
                raw_title_candidate=mention.raw_text,
                article_raw=article.article if article else None,
                paragraphs_raw=list(article.paragraphs) if article else [],
                items_raw=list(article.items) if article else [],
                recognition_form="bare",
                mention_span=mention.resolution_anchor_span,
            ))
    for case_index, case in enumerate(getattr(entities, "case_refs", []), 1):
        case_mentions.append(RawCaseMention(
            mention_id=f"{claim.claim_id}:case:{case_index}",
            raw_case_number=case.case_number,
            raw_case_name=case.case_name,
            raw_court=case.court,
            raw_document_type=case.document_type,
        ))
    return RawClaim(
        claim_id=claim.claim_id,
        claim_type=claim.claim_type,
        raw_text=claim.text,
        context_text=claim.context_text,
        anchor_ids=claim.anchor_ids,
        source_locations=claim.source_locations,
        note_context=claim.note_context,
        legal_mentions=legal_mentions,
        case_mentions=case_mentions,
        alias_declarations=list(getattr(entities, "alias_declarations", [])),
        structural_relations=[
            ClaimRelation.model_validate(item)
            for item in getattr(entities, "structural_relations", [])
        ],
    )


def _inherited_from(source) -> InheritedFrom | None:
    inherited = source.recognition.inherited_from
    if inherited is None:
        return None
    return InheritedFrom(
        anchor_id=inherited.anchor_id,
        source_location=inherited.source_location,
    )


def from_legacy_document(document: ClaimDocument) -> RawClaimDocument:
    return RawClaimDocument(
        source_claim_doc_id=document.claim_meta.claim_doc_id,
        source_doc_id=document.claim_meta.source_doc_id,
        document_text=document.document_text,
        claims=[from_legacy_claim(claim) for claim in document.claims],
    )


__all__ = [
    "AliasDeclaration",
    "ClaimRelation",
    "InheritedFrom",
    "RawCaseMention",
    "RawCitationLocator",
    "RawClaim",
    "RawClaimDocument",
    "RawLegalMention",
    "from_legacy_claim",
    "from_legacy_document",
]
