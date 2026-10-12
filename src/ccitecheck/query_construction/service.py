"""从 RawClaim 构造可追溯的 SearchHypothesis。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from threading import Lock

from ..domain.citation import AliasDeclaration, ClaimType
from ..domain.claims import RawClaim, RawClaimDocument, RawLegalMention
from ..domain.queries import (
    HypothesisAssumption,
    HypothesisFeedback,
    IdentityCandidate,
    JurisdictionCandidate,
    NormalizedLocator,
    ProviderStrategy,
    QueryExtractionFill,
    QueryInference,
    RetrievalQueryPlan,
    SearchHypothesis,
    SourcePlanItem,
    VersionHypothesis,
)
from ..infrastructure.database import normalize_title
from ..domain.law_titles import canonical_cn_title_shape, cn_title_shape_key
from .aliases import alias_map
from .jurisdiction import detect_jurisdiction_with_basis
from .locator import normalize_locator
from .name_resolution import LawLexicon
from .normalization import normalize_identity_title
from .planners import RelatedPlanner, fuzzy_title_candidates, primary_plan
from .source_planner import plan_sources
from .strategies.cases import case_strategies
from .strategies.statutes import statute_strategies
from .versioning import build_version_hypothesis


@dataclass
class QueryResources:
    law_db: str | Path | None = None
    lexicon: LawLexicon | None = None
    _canonical_titles: tuple[str, ...] | None = field(
        default=None, init=False, repr=False
    )
    _fuzzy_cache: dict[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False
    )
    _fuzzy_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def law_lexicon(self) -> LawLexicon:
        return self.lexicon or LawLexicon.load(self.law_db)

    def fuzzy_titles(self, raw_title: str) -> tuple[str, ...]:
        """同一文档内每个原始法名最多执行一次全库模糊匹配。"""
        key = normalize_title(raw_title)
        with self._fuzzy_lock:
            cached = self._fuzzy_cache.get(key)
            if cached is not None:
                return cached
            if self._canonical_titles is None:
                self._canonical_titles = tuple(dict.fromkeys(
                    entry.canonical_title for entry in self.law_lexicon().entries
                ))
            candidates = tuple(fuzzy_title_candidates(
                raw_title, list(self._canonical_titles)
            ))
            self._fuzzy_cache[key] = candidates
            return candidates


@dataclass
class DocumentQueryContext:
    """整份文档的简称可见性与唯一承前法源。"""

    declarations_by_claim: dict[str, tuple[AliasDeclaration, ...]]
    sources_by_anchor: dict[str, tuple[RawClaim, RawLegalMention]]
    resolved_by_anchor: dict[str, SearchHypothesis] = field(default_factory=dict)
    inference_by_source: dict[
        tuple[str, str], tuple[QueryInference | None, str | None]
    ] = field(default_factory=dict)

    def declarations_for(self, claim_id: str) -> tuple[AliasDeclaration, ...]:
        return self.declarations_by_claim.get(claim_id, ())

    def inherited_source(
        self, mention: RawLegalMention | None
    ) -> tuple[RawClaim, RawLegalMention] | None:
        anchor_id = (
            mention.inherited_from.anchor_id
            if mention and mention.inherited_from else None
        )
        return self.sources_by_anchor.get(anchor_id or "")

    def inherited_hypothesis(
        self, mention: RawLegalMention | None
    ) -> SearchHypothesis | None:
        anchor_id = (
            mention.inherited_from.anchor_id
            if mention and mention.inherited_from else None
        )
        return self.resolved_by_anchor.get(anchor_id or "")

    def remember(
        self,
        claim: RawClaim,
        mention: RawLegalMention | None,
        hypothesis: SearchHypothesis,
    ) -> None:
        if mention is None or mention.recognition_form == "inherited":
            return
        for anchor_id in claim.anchor_ids:
            source = self.sources_by_anchor.get(anchor_id)
            if source and source[0].claim_id == claim.claim_id:
                self.resolved_by_anchor.setdefault(anchor_id, hypothesis)


def build_document_context(document: RawClaimDocument) -> DocumentQueryContext:
    """按文档顺序建立简称表，并只为单一法源 anchor 建承前索引。"""
    visible: list[AliasDeclaration] = []
    declarations_by_claim: dict[str, tuple[AliasDeclaration, ...]] = {}
    sources_by_anchor: dict[str, tuple[RawClaim, RawLegalMention]] = {}
    for claim in document.claims:
        if claim.claim_type != ClaimType.LEGAL_SOURCE_CLAIM:
            continue
        visible.extend(claim.alias_declarations)
        declarations_by_claim[claim.claim_id] = tuple(visible)
        direct = [
            mention for mention in claim.legal_mentions
            if mention.recognition_form != "inherited"
            and (mention.raw_title or mention.raw_title_candidate)
        ]
        titles = {
            normalize_title(mention.raw_title or mention.raw_title_candidate or "")
            for mention in direct
        }
        if len(titles) != 1 or not direct:
            continue
        for anchor_id in claim.anchor_ids:
            sources_by_anchor.setdefault(anchor_id, (claim, direct[0]))
    return DocumentQueryContext(declarations_by_claim, sources_by_anchor)


def _mention(claim: RawClaim, mention_id: str | None) -> RawLegalMention | None:
    if mention_id is None:
        return claim.legal_mentions[0] if claim.legal_mentions else None
    return next((item for item in claim.legal_mentions if item.mention_id == mention_id), None)


def _identities(
    claim: RawClaim,
    mention: RawLegalMention | None,
    resources: QueryResources,
    document_context: DocumentQueryContext | None = None,
) -> list[IdentityCandidate]:
    if mention is None:
        case = claim.case_mentions[0] if claim.case_mentions else None
        raw = (case.raw_case_number or case.raw_case_name) if case else None
        return [IdentityCandidate(title=raw, basis="raw", priority=1)] if raw else []

    inherited_hypothesis = (
        document_context.inherited_hypothesis(mention) if document_context else None
    )
    if inherited_hypothesis and inherited_hypothesis.identity_candidates:
        return list(inherited_hypothesis.identity_candidates)
    inherited = document_context.inherited_source(mention) if document_context else None
    source_mention = inherited[1] if inherited else mention
    raw = (
        source_mention.raw_title or source_mention.raw_title_candidate or ""
    ).strip() if source_mention else ""
    if not raw:
        return []
    declarations = (
        document_context.declarations_for(claim.claim_id)
        if document_context else tuple(claim.alias_declarations)
    )
    return _resolve_identity_candidates(
        raw,
        resources,
        declarations=declarations,
        raw_title_candidate=source_mention.raw_title_candidate,
    )


def _jurisdiction(
    claim: RawClaim,
    mention: RawLegalMention | None,
    identities: list[IdentityCandidate],
    document_context: DocumentQueryContext | None,
) -> tuple[str, list[str]]:
    if mention and mention.jurisdiction:
        return mention.jurisdiction, ["recognition_jurisdiction"]
    inherited_hypothesis = (
        document_context.inherited_hypothesis(mention) if document_context else None
    )
    if inherited_hypothesis:
        return inherited_hypothesis.jurisdiction.code, ["inherited_hypothesis"]
    inherited = document_context.inherited_source(mention) if document_context else None
    source_claim, source_mention = inherited or (claim, mention)
    if source_mention and source_mention.jurisdiction:
        return source_mention.jurisdiction, ["inherited_jurisdiction"]
    title = next((
        item.title for item in reversed(identities)
        if item.basis in {"document_alias", "lexicon"}
    ), identities[0].title if identities else "")
    span = source_mention.mention_span if source_mention else None
    preceding = source_claim.raw_text[:span[0]] if span is not None else ""
    jurisdiction, basis = detect_jurisdiction_with_basis(title, preceding)
    return jurisdiction, ["inherited_source" if inherited else basis]


_VERSION_SEMANTIC_CUE = re.compile(
    r"现行|(?:当时|届时)有效|"
    r"(?<!\d)(?:19|20)\d{2}\s*年?\s*(?:修正|修订|修改)|"
    r"(?:修正|修订|修改)(?:前|后|版本)"
)


def _version_signal_for_mention(
    claim: RawClaim, mention: RawLegalMention
) -> str | None:
    """把同一分句中的版本表达只分配给最近的 mention。"""
    cues = list(_VERSION_SEMANTIC_CUE.finditer(claim.raw_text))
    if not cues:
        return None
    positioned = [
        item for item in claim.legal_mentions if item.mention_span is not None
    ]
    if mention.mention_span is None:
        return cues[0].group(0) if len(claim.legal_mentions) == 1 else None

    def distance(cue, item: RawLegalMention) -> int:
        start, end = item.mention_span or (0, 0)
        if cue.end() <= start:
            bridge = claim.raw_text[cue.end():start]
            value = start - cue.end()
        elif end <= cue.start():
            bridge = claim.raw_text[end:cue.start()]
            value = cue.start() - end
        else:
            return 0
        return (
            10_000
            if value > 80 or re.search(r"[。！？；\n]", bridge)
            else value
        )

    for cue in cues:
        candidates = []
        for index, item in enumerate(positioned):
            item_distance = distance(cue, item)
            if item_distance < 10_000:
                candidates.append((item_distance, index, item))
        owner = min(candidates, default=None)
        owner = owner[2] if owner else None
        if owner is not None and (
            owner.mention_id == mention.mention_id
            or (
                owner.mention_span == mention.mention_span
                and normalize_title(owner.raw_title or owner.raw_title_candidate or "")
                == normalize_title(
                    mention.raw_title or mention.raw_title_candidate or ""
                )
            )
        ):
            return (
                mention.raw_time
                if mention.raw_time and cue.group(0) in mention.raw_time
                else cue.group(0)
            )
    return None


def _missing_fields(
    claim: RawClaim,
    mention: RawLegalMention,
    resources: QueryResources,
) -> list[str]:
    missing: list[str] = []
    raw = mention.raw_title or mention.raw_title_candidate or ""
    if not raw and mention.recognition_form != "inherited":
        missing.extend(["raw_title", "raw_title_candidate"])
    elif (
        mention.recognition_form == "bare"
        and mention.raw_title is None
        and resources.law_lexicon().longest_suffix_match(raw) is None
    ):
        missing.append("raw_title")
    if mention.article_raw is None and re.search(r"第[^，。；\s]{1,20}条", claim.raw_text):
        missing.append("article_raw")
    if not mention.paragraphs_raw and re.search(r"第[^，。；\s]{1,12}款", claim.raw_text):
        missing.append("paragraphs_raw")
    if not mention.items_raw and re.search(r"第[^，。；\s]{1,12}项", claim.raw_text):
        missing.append("items_raw")
    return missing


def _validated_fill(
    claim_text: str,
    mention: RawLegalMention,
    fill: QueryExtractionFill,
    missing_fields: list[str],
) -> RawLegalMention:
    updates = {}
    for name in missing_fields:
        current = getattr(mention, name)
        if current not in (None, [], ""):
            continue
        value = getattr(fill, name)
        if isinstance(value, str):
            if value and value in claim_text:
                updates[name] = value
        elif isinstance(value, list) and all(
            isinstance(item, str) and item and item in claim_text for item in value
        ):
            updates[name] = value
    return mention.model_copy(update=updates)


def _fill_missing_facts(
    claim: RawClaim,
    mention: RawLegalMention | None,
    resources: QueryResources,
    planner,
) -> tuple[RawLegalMention | None, str | None]:
    if mention is None:
        return None, None
    missing = _missing_fields(claim, mention, resources)
    extract = getattr(planner, "extract_query_facts", None)
    if not missing or not callable(extract):
        return mention, None
    try:
        fill = QueryExtractionFill.model_validate(extract(
            claim_text=claim.raw_text,
            mention=mention.model_dump(mode="json"),
            missing_fields=missing,
        ))
    except Exception as exc:
        return mention, f"query_extraction_failed: {exc}"
    return _validated_fill(claim.raw_text, mention, fill, missing), None


def _inference_context(
    claim: RawClaim,
    mention: RawLegalMention,
    document_context: DocumentQueryContext | None,
) -> tuple[dict | None, dict | None]:
    declarations = (
        document_context.declarations_for(claim.claim_id)
        if document_context else tuple(claim.alias_declarations)
    )
    raw = normalize_title(mention.raw_title or mention.raw_title_candidate or "")
    alias = next((
        item for item in reversed(declarations)
        if normalize_title(item.alias_raw) == raw
    ), None)
    alias_context = (
        {"alias": alias.alias_raw, "full_title": alias.full_name_raw}
        if alias else None
    )
    inherited = document_context.inherited_hypothesis(mention) if document_context else None
    inherited_context = None
    if inherited:
        inherited_context = {
            "title": (
                inherited.query_plan.target_name
                if inherited.query_plan and inherited.query_plan.target_name
                else inherited.identity_candidates[-1].title
            ),
            "jurisdiction": inherited.jurisdiction.code,
            "raw_time": (
                inherited.version_hypothesis.temporal_reference
                if inherited.version_hypothesis else None
            ),
        }
    return alias_context, inherited_context


def _valid_jurisdiction(value: str) -> bool:
    return bool(re.fullmatch(r"(?:CN|EU|UN|UNKNOWN|FOREIGN|[A-Z]{2}(?:-[A-Z0-9]{1,3})?)", value))


def resolve_identity_candidates(
    raw_title: str,
    resources: QueryResources,
    *,
    declarations=(),
    raw_title_candidate: str | None = None,
) -> list[IdentityCandidate]:
    """兼容公开入口；与文档级构造共用同一法名解释核心。"""
    return _resolve_identity_candidates(
        raw_title,
        resources,
        declarations=declarations,
        raw_title_candidate=raw_title_candidate,
    )


def _resolve_identity_candidates(
    raw_title: str,
    resources: QueryResources,
    *,
    declarations=(),
    raw_title_candidate: str | None = None,
) -> list[IdentityCandidate]:
    raw = (raw_title or raw_title_candidate or "").strip()
    if not raw:
        return []
    aliases = alias_map(list(declarations))
    alias_title = aliases.get(normalize_title(raw))
    lexicon = resources.law_lexicon()
    resolved = alias_title or lexicon.canonical_title_for(raw)
    basis = "document_alias" if alias_title else "lexicon"
    if resolved is None and raw_title_candidate:
        matched = lexicon.longest_suffix_match(raw_title_candidate)
        if matched:
            resolved = matched.canonical_title
    return _identity_candidates(raw, resolved, basis)


def _identity_candidates(
    raw: str, resolved: str | None, resolved_basis: str
) -> list[IdentityCandidate]:
    result = [IdentityCandidate(title=raw, basis="raw", priority=1)]
    target = resolved or canonical_cn_title_shape(raw)
    if normalize_title(target) != normalize_title(raw):
        result.append(IdentityCandidate(
            title=target,
            basis=resolved_basis if resolved else "title_shape",
            priority=2,
        ))
    return result


def _inference_cache_key(
    claim: RawClaim, mention: RawLegalMention
) -> tuple[str, str]:
    inherited_anchor = (
        mention.inherited_from.anchor_id if mention.inherited_from else None
    )
    source = inherited_anchor or normalize_title(
        mention.raw_title or mention.raw_title_candidate or mention.mention_id
    )
    return claim.claim_id, source


def _run_inference(
    claim: RawClaim,
    mention: RawLegalMention | None,
    resources: QueryResources,
    planner,
    document_context: DocumentQueryContext | None,
    identities: list[IdentityCandidate],
    jurisdiction_code: str,
    jurisdiction_basis: list[str],
    version_hypothesis: VersionHypothesis | None,
) -> tuple[
    list[IdentityCandidate], str, list[str], VersionHypothesis | None, str | None
]:
    inference = getattr(planner, "infer_query", None)
    cache_key = _inference_cache_key(claim, mention) if mention else None
    cached = (
        document_context.inference_by_source.get(cache_key)
        if document_context is not None and cache_key is not None else None
    )
    inherited = (
        document_context.inherited_hypothesis(mention)
        if document_context else None
    )
    identity_resolved = bool(inherited) or any(
        item.basis in {"document_alias", "lexicon"} for item in identities
    )
    weak_default_cn = (
        jurisdiction_basis == ["default_cn"]
        and not any(item.basis == "lexicon" for item in identities)
    )
    version_signal = (
        _version_signal_for_mention(claim, mention) if mention else None
    )
    needs_inference = bool(
        mention
        and callable(inference)
        and (
            cached is not None
            or not identity_resolved
            or jurisdiction_code == "UNKNOWN"
            or weak_default_cn
            or (
                version_hypothesis is None
                and version_signal is not None
            )
        )
    )
    if not needs_inference or mention is None:
        return (
            identities, jurisdiction_code, jurisdiction_basis,
            version_hypothesis, None,
        )

    raw = mention.raw_title or mention.raw_title_candidate or ""
    allowed_titles = list(dict.fromkeys([
        *(item.title for item in identities),
        *resources.fuzzy_titles(raw),
    ]))
    alias_context, inherited_context = _inference_context(
        claim, mention, document_context
    )
    if cached is not None:
        inferred, inference_error = cached
    else:
        try:
            inferred = QueryInference.model_validate(inference(
                claim_text=claim.raw_text,
                context_text=claim.context_text,
                extracted=mention.model_copy(
                    update={"raw_time": version_signal}
                ).model_dump(mode="json"),
                alias_context=alias_context,
                inherited_context=inherited_context,
                allowed_title_candidates=allowed_titles,
            ))
            inference_error = None
        except Exception as exc:
            inferred = None
            inference_error = f"query_inference_failed: {exc}"
        if document_context is not None and cache_key is not None:
            document_context.inference_by_source[cache_key] = (
                inferred, inference_error
            )
    if inference_error is not None or inferred is None:
        return (
            identities, jurisdiction_code, jurisdiction_basis,
            version_hypothesis, inference_error,
        )
    try:
        allowed = {
            cn_title_shape_key(normalize_title(item)): item
            for item in allowed_titles if item
        }
        inferred_key = (
            cn_title_shape_key(normalize_title(inferred.canonical_title))
            if inferred.canonical_title else None
        )
        if inferred_key in allowed and not any(
            cn_title_shape_key(normalize_title(item.title)) == inferred_key
            for item in identities
        ):
            identities.append(IdentityCandidate(
                title=allowed[inferred_key],
                basis="llm_candidate",
                priority=len(identities) + 1,
            ))
        if (
            inferred.jurisdiction != "UNKNOWN"
            and _valid_jurisdiction(inferred.jurisdiction)
            and (
                (jurisdiction_code == "UNKNOWN" and jurisdiction_basis != [
                    "conflicting_explicit_signals"
                ])
                or weak_default_cn
            )
        ):
            jurisdiction_code = inferred.jurisdiction
            jurisdiction_basis = ["llm_inference_from_weak_signal"]
        if version_hypothesis is None and inferred.version_kind != "unknown":
            year = inferred.version_year
            if year is not None and str(year) not in claim.raw_text + claim.context_text:
                year = None
            version_hypothesis = VersionHypothesis(
                year=year,
                kind=inferred.version_kind,
                temporal_reference=version_signal or claim.raw_text,
            )
    except Exception as exc:
        return (
            identities, jurisdiction_code, jurisdiction_basis,
            version_hypothesis, f"query_inference_apply_failed: {exc}",
        )
    return identities, jurisdiction_code, jurisdiction_basis, version_hypothesis, None


def _finalize_query_plan(
    plan: RetrievalQueryPlan,
    mention: RawLegalMention | None,
    jurisdiction_code: str,
    version: VersionHypothesis | None,
) -> RetrievalQueryPlan:
    if plan.version_hint is None and version is not None and (
        version.kind == "current" or version.year is not None
    ):
        plan = plan.model_copy(update={
            "version_hint": "current" if version.kind == "current" else str(version.year)
        })
    # 涉外标题保持原文，不套用中国法名的“中华人民共和国”形态补全。
    if plan.route.startswith("statute_") and jurisdiction_code != "CN" and mention:
        raw_title = mention.raw_title or mention.raw_title_candidate
        if raw_title:
            plan = plan.model_copy(update={"target_name": raw_title})
    return plan


def _build_strategies(
    claim: RawClaim,
    jurisdiction_code: str,
    locator: NormalizedLocator | None,
    query_plan: RetrievalQueryPlan,
) -> tuple[list[SourcePlanItem], list[ProviderStrategy]]:
    if query_plan.route == "skip":
        return [], []
    if claim.case_mentions:
        source_plan = [] if jurisdiction_code != "CN" else [SourcePlanItem(
            source_id="pkulaw_cases",
            priority=1,
            reason="cn_case_authoritative_source",
        )]
        return source_plan, case_strategies(source_plan)
    source_plan = plan_sources(jurisdiction_code)
    return source_plan, statute_strategies(source_plan, locator)


def build_initial_hypothesis(
    claim: RawClaim,
    resources: QueryResources,
    *,
    mention_id: str | None = None,
    planner: RelatedPlanner | None = None,
    document_context: DocumentQueryContext | None = None,
) -> SearchHypothesis:
    mention, extraction_error = _fill_missing_facts(
        claim, _mention(claim, mention_id), resources, planner
    )
    identities = _identities(claim, mention, resources, document_context)
    title = identities[0].title if identities else ""
    jurisdiction_code, jurisdiction_basis = _jurisdiction(
        claim, mention, identities, document_context
    )
    version_signal = (
        _version_signal_for_mention(claim, mention) if mention else None
    )
    version_hypothesis = (
        build_version_hypothesis(title, version_signal)
        if title and not claim.case_mentions else None
    )
    inherited_hypothesis = (
        document_context.inherited_hypothesis(mention)
        if document_context else None
    )
    if version_hypothesis is None and inherited_hypothesis:
        version_hypothesis = inherited_hypothesis.version_hypothesis
    (
        identities,
        jurisdiction_code,
        jurisdiction_basis,
        version_hypothesis,
        inference_error,
    ) = _run_inference(
        claim,
        mention,
        resources,
        planner,
        document_context,
        identities,
        jurisdiction_code,
        jurisdiction_basis,
        version_hypothesis,
    )
    jurisdiction = JurisdictionCandidate(
        code=jurisdiction_code,
        basis=jurisdiction_basis,
    )
    locator = (
        normalize_locator(
            mention.article_raw,
            mention.paragraphs_raw,
            mention.items_raw,
            mention.structures_raw,
        )
        if mention else None
    )
    routing_mention = (
        mention.model_copy(update={"raw_time": version_signal})
        if mention else None
    )
    query_plan, planner_error = primary_plan(
        claim, routing_mention, identities, locator, planner
    )
    query_plan = _finalize_query_plan(
        query_plan, mention, jurisdiction_code, version_hypothesis
    )
    source_plan, strategies = _build_strategies(
        claim, jurisdiction_code, locator, query_plan
    )
    hypothesis = SearchHypothesis(
        claim_id=claim.claim_id,
        mention_id=mention.mention_id if mention else None,
        jurisdiction=jurisdiction,
        identity_candidates=identities,
        version_hypothesis=version_hypothesis,
        normalized_locator=locator,
        source_plan=source_plan,
        provider_strategies=strategies,
        assumptions=[
            HypothesisAssumption(
                code="identity_order",
                value=[item.title for item in identities],
                basis="raw_then_document_alias_then_lexicon",
            ),
            *(
                [HypothesisAssumption(
                    code="planner_failure",
                    value=planner_error,
                    basis="related_planner",
                )]
                if planner_error else []
            ),
            *(
                [HypothesisAssumption(
                    code="extraction_failure",
                    value=extraction_error,
                    basis="deterministic_fallback",
                )]
                if extraction_error else []
            ),
            *(
                [HypothesisAssumption(
                    code="inference_failure",
                    value=inference_error,
                    basis="deterministic_fallback",
                )]
                if inference_error else []
            ),
        ],
        query_plan=query_plan,
        trigger_reason="initial",
    )
    if document_context:
        document_context.remember(claim, mention, hypothesis)
    return hypothesis


def rebuild_hypothesis(
    claim: RawClaim,
    previous: SearchHypothesis,
    feedback: HypothesisFeedback,
    resources: QueryResources,
) -> SearchHypothesis:
    """只应用检索/repair 已验证的反馈，不重复执行原文提取和语义推断。"""
    identities = list(previous.identity_candidates)
    if feedback.identity_title:
        title = normalize_identity_title(feedback.identity_title)
        identities = [
            IdentityCandidate(title=title, basis=feedback.basis, priority=1),
            *[
                item.model_copy(update={"priority": index + 2})
                for index, item in enumerate(identities)
                if cn_title_shape_key(normalize_title(item.title))
                != cn_title_shape_key(normalize_title(title))
            ],
        ]
    locator = previous.normalized_locator
    if feedback.article_raw:
        locator = normalize_locator(
            feedback.article_raw,
            [locator.paragraph_raw] if locator and locator.paragraph_raw else [],
            [locator.item_raw] if locator and locator.item_raw else [],
            locator.structure_labels if locator else [],
        )
    source_plan = plan_sources(previous.jurisdiction.code)
    return SearchHypothesis(
        claim_id=claim.claim_id,
        mention_id=previous.mention_id,
        parent_hypothesis_id=previous.hypothesis_id,
        attempt_no=previous.attempt_no + 1,
        jurisdiction=previous.jurisdiction,
        identity_candidates=identities,
        version_hypothesis=(
            previous.version_hypothesis.model_copy(update={"year": feedback.version_year})
            if feedback.version_year and previous.version_hypothesis
            else (
                VersionHypothesis(
                    year=feedback.version_year,
                    temporal_reference=feedback.basis,
                )
                if feedback.version_year else previous.version_hypothesis
            )
        ),
        normalized_locator=locator,
        source_plan=source_plan,
        provider_strategies=statute_strategies(source_plan, locator),
        assumptions=[*previous.assumptions, HypothesisAssumption(
            code=feedback.trigger_reason,
            value=feedback.model_dump(exclude_none=True),
            basis=feedback.basis,
        )],
        query_plan=(
            previous.query_plan.model_copy(update={
                "target_name": identities[0].title if identities else None,
                "article_no": locator.article_raw if locator else None,
                "query_text": None,
                "route": "statute_exact" if locator and locator.article_raw else "statute_related",
            })
            if previous.query_plan else None
        ),
        trigger_reason=feedback.trigger_reason,
    )


__all__ = [
    "DocumentQueryContext",
    "QueryResources",
    "build_document_context",
    "build_initial_hypothesis",
    "rebuild_hypothesis",
    "resolve_identity_candidates",
]
