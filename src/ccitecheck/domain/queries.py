"""可追溯检索假设模型。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator


class JurisdictionCandidate(BaseModel):
    code: str
    score: float | None = Field(default=None, ge=0, le=1)
    basis: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


class IdentityCandidate(BaseModel):
    title: str
    basis: str
    priority: int = Field(ge=1)
    source_url: str | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


class VersionHypothesis(BaseModel):
    year: int | None = None
    kind: Literal["amended", "revised", "historical", "current", "unknown"] = "unknown"
    temporal_reference: str | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)


class NormalizedLocator(BaseModel):
    article_number: int | None = None
    article_raw: str | None = None
    paragraph_number: int | None = None
    paragraph_raw: str | None = None
    item_number: int | None = None
    item_raw: str | None = None
    structure_labels: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourcePlanItem(BaseModel):
    source_id: str
    priority: int = Field(ge=1)
    reason: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProviderStrategy(BaseModel):
    source_id: str
    goal: str
    preferred_mode: str
    allowed_fallbacks: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


class HypothesisAssumption(BaseModel):
    code: str
    value: Any = None
    basis: str
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateArticlePlan(BaseModel):
    law_title: str = Field(min_length=1)
    article_no: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalQueryPlan(BaseModel):
    route: Literal["statute_exact", "statute_related", "case", "skip"]
    target_name: str | None = None
    article_no: str | None = None
    query_text: str | None = Field(default=None, max_length=300)
    version_hint: str | None = Field(default=None, max_length=40)
    candidate_articles: list[CandidateArticlePlan] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_route(self):
        if self.route == "statute_related" and (
            not self.target_name or self.article_no is not None
        ):
            raise ValueError("statute_related requires target_name and no article_no")
        if self.route == "statute_exact" and (
            not self.target_name or not self.article_no or self.query_text is not None
        ):
            raise ValueError("statute_exact requires target_name/article_no only")
        if self.route == "skip" and self.candidate_articles:
            raise ValueError("skip must not include candidate_articles")
        if self.route != "statute_related" and self.candidate_articles:
            raise ValueError("candidate_articles are only allowed for statute_related")
        return self


class RepairDiagnosis(BaseModel):
    reason: Literal[
        "title_typo", "title_alias", "article_error", "version_issue",
        "source_gap", "non_normative", "unknown",
    ]
    confidence: float = Field(ge=0, le=1)
    message: str = Field(min_length=1, max_length=300)
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepairPlan(BaseModel):
    diagnosis: RepairDiagnosis
    retry_request: RetrievalQueryPlan | None = None
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_retry(self):
        if self.retry_request and self.retry_request.route != "statute_exact":
            raise ValueError("repair retry_request must use statute_exact")
        return self


class RerankCandidate(BaseModel):
    candidate_id: str
    law_title: str
    article_no: str = ""
    locator: str | None = None
    locator_type: Literal["article", "paragraph"] = "article"
    article_text: str = Field(min_length=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class RerankDecision(BaseModel):
    candidate_id: str
    relevant: bool
    score: float = Field(ge=0, le=1)
    model_config = ConfigDict(extra="forbid", frozen=True)


class RerankBatch(BaseModel):
    results: list[RerankDecision] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


class FidelityChecks(BaseModel):
    subject: bool
    condition: bool
    numbers: bool
    negation: bool
    consequence: bool
    model_config = ConfigDict(extra="forbid", frozen=True)


class FidelityTriage(BaseModel):
    verdict: str = Field(min_length=1)
    checks: FidelityChecks
    differences: list[str] = Field(default_factory=list, max_length=5)
    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="after")
    def validate_result(self, info: ValidationInfo):
        allowed = (info.context or {}).get("allowed_verdicts")
        if allowed is not None and self.verdict not in allowed:
            raise ValueError("verdict must reference an input source id")
        faithful = all(self.checks.model_dump().values())
        if self.verdict == "none":
            if not self.differences or any(not item.strip() for item in self.differences):
                raise ValueError("none verdict requires a concrete difference")
        elif not faithful or self.differences:
            raise ValueError("selected source requires all checks and no differences")
        return self


class QueryExtractionFill(BaseModel):
    raw_title: str | None = None
    raw_title_candidate: str | None = None
    raw_time: str | None = None
    article_raw: str | None = None
    paragraphs_raw: list[str] = Field(default_factory=list)
    items_raw: list[str] = Field(default_factory=list)
    structures_raw: list[str] = Field(default_factory=list)
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryInference(BaseModel):
    canonical_title: str | None = None
    jurisdiction: str = "UNKNOWN"
    version_kind: Literal[
        "amended", "revised", "historical", "current", "unknown"
    ] = "unknown"
    version_year: int | None = Field(default=None, ge=1800, le=2200)
    model_config = ConfigDict(extra="forbid", frozen=True)


class SearchHypothesis(BaseModel):
    hypothesis_id: str = Field(default_factory=lambda: str(uuid4()))
    claim_id: str
    mention_id: str | None = None
    parent_hypothesis_id: str | None = None
    attempt_no: int = Field(default=1, ge=1)
    jurisdiction: JurisdictionCandidate
    identity_candidates: list[IdentityCandidate] = Field(default_factory=list)
    version_hypothesis: VersionHypothesis | None = None
    normalized_locator: NormalizedLocator | None = None
    source_plan: list[SourcePlanItem] = Field(default_factory=list)
    provider_strategies: list[ProviderStrategy] = Field(default_factory=list)
    assumptions: list[HypothesisAssumption] = Field(default_factory=list)
    query_plan: RetrievalQueryPlan | None = None
    trigger_reason: str = "initial"
    model_config = ConfigDict(extra="forbid", frozen=True)


class HypothesisFeedback(BaseModel):
    trigger_reason: str
    identity_title: str | None = None
    article_raw: str | None = None
    version_year: int | None = None
    basis: str
    model_config = ConfigDict(extra="forbid", frozen=True)


__all__ = [
    "CandidateArticlePlan",
    "HypothesisAssumption",
    "HypothesisFeedback",
    "IdentityCandidate",
    "FidelityChecks",
    "FidelityTriage",
    "JurisdictionCandidate",
    "NormalizedLocator",
    "ProviderStrategy",
    "QueryExtractionFill",
    "QueryInference",
    "RepairDiagnosis",
    "RepairPlan",
    "RerankBatch",
    "RerankCandidate",
    "RerankDecision",
    "RetrievalQueryPlan",
    "SearchHypothesis",
    "SourcePlanItem",
    "VersionHypothesis",
]
