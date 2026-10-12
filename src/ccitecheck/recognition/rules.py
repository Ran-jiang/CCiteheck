"""基于 anchor 内容和显式 Block 关系的确定性引用抽取。"""

from __future__ import annotations

import re

from ..domain.citation import (
    AliasDeclaration,
    CaseCitationEntities,
    CaseHoldingParaphraseEntities,
    ClaimCandidate,
    ClaimType,
    LegalSourceClaimEntities,
    LegalSourceRecognition,
)
from ..domain.document import BlockRelationType, BlockType, ParsedDocument
from ..parsing.relations import build_block_relations
from .cases import extract_case_refs, has_holding_trigger
from .statutes import (
    extract_alias_declarations,
    extract_articles_only,
    extract_legal_sources,
    extract_partial_refs,
    has_article_reference,
)


_RELATION_PRIORITY = {
    BlockRelationType.TABLE_LEFT: 0,
    BlockRelationType.LIST_LEAD: 1,
    BlockRelationType.PREVIOUS_BLOCK: 2,
    BlockRelationType.TABLE_ABOVE: 3,
}

_QUOTED_ANCHOR = re.compile(r"^[“‘\"].+[”’\"]\s*$")
_CITATION_AFTER_QUOTE = re.compile(r"^\s*(?:[-—–]+\s*)?《")
_CASE_FOLLOWUP = re.compile(r"^(?:涉案|案涉|法院|该案|本案|因此|裁判)")
_CASE_HOLDING_FOLLOWUP = re.compile(r"(?:法院认为|裁判(?:认为|规则|要旨)|该案.*(?:认为|指出|明确|确立|规则)|充分依据)")


def extract_rule_candidates(
    parsed_doc: ParsedDocument,
    indexes: dict,
    include_statutes: bool = True,
    include_cases: bool = True,
) -> list[ClaimCandidate]:
    """逐句生成候选；裸条款只沿显式 Block 关系承前。"""
    build_block_relations(parsed_doc)
    candidates: list[ClaimCandidate] = []
    block_map = indexes.get("block_map", {})
    anchor_sources = {
        anchor.anchor: extract_legal_sources(anchor.text) if include_statutes else []
        for anchor in parsed_doc.anchors
    }
    records: dict[str, list[tuple[str, list]]] = {}
    previous_anchor = None
    for anchor in parsed_doc.anchors:
        if anchor_sources[anchor.anchor]:
            records.setdefault(anchor.block_id, []).append(
                (anchor.anchor, anchor_sources[anchor.anchor])
            )
    deferred_table_sources: dict[str, ClaimCandidate] = {}
    consumed_table_sources: set[str] = set()

    for anchor in parsed_doc.anchors:
        text = anchor.text
        current_block = block_map.get(anchor.block_id)
        legal_sources = anchor_sources[anchor.anchor]

        if legal_sources:
            candidate = _make_legal_candidate(
                anchor.anchor,
                legal_sources,
                extract_alias_declarations(text),
            )
            if candidate:
                candidate.anchor_ids = _legal_claim_anchor_ids(
                    anchor, current_block, parsed_doc, anchor_sources
                )
                if (
                    previous_anchor is not None
                    and _QUOTED_ANCHOR.fullmatch(previous_anchor.text)
                    and _CITATION_AFTER_QUOTE.match(text)
                    and _anchors_are_structurally_adjacent(
                        previous_anchor, anchor, block_map
                    )
                ):
                    candidate.anchor_ids = [previous_anchor.anchor, anchor.anchor]
                    offset = len(previous_anchor.text)
                    for source in legal_sources:
                        span = source.recognition.mention_span
                        if span is not None:
                            source.recognition.mention_span = (
                                span[0] + offset,
                                span[1] + offset,
                            )
                if (
                    current_block
                    and current_block.type == BlockType.TABLE_CELL
                    and all(not source.articles for source in legal_sources)
                ):
                    deferred_table_sources[anchor.anchor] = candidate
                else:
                    candidates.append(candidate)

        if (
            not legal_sources
            and current_block
            and has_article_reference(text)
        ):
            articles = extract_articles_only(text)
            resolved = _resolve_block_source(
                current_block, block_map, records, current_anchor_id=anchor.anchor
            )
            if not articles and resolved:
                articles = _merge_partial_with_inherited_article(
                    extract_partial_refs(text), resolved[2]
                )
            if articles and resolved:
                source_anchor_id, source_block, sources = resolved
                inherited_sources = _build_inherited_sources(
                    sources, articles, source_anchor_id
                )
                candidate = _make_legal_candidate(anchor.anchor, inherited_sources)
                if candidate:
                    if source_block.type == BlockType.TABLE_CELL:
                        candidate.anchor_ids = [source_anchor_id, anchor.anchor]
                        consumed_table_sources.add(source_anchor_id)
                    candidates.append(candidate)
                    records.setdefault(anchor.block_id, []).append(
                        (anchor.anchor, inherited_sources)
                    )

        case_refs = extract_case_refs(text) if include_cases else []
        if case_refs:
            case_anchor_ids = _case_holding_anchor_ids(
                anchor, current_block, parsed_doc, anchor_sources
            )
            if len(case_anchor_ids) > 1 or has_holding_trigger(text, case_refs):
                candidates.append(ClaimCandidate(
                    claim_type=ClaimType.CASE_HOLDING_PARAPHRASE,
                    anchor_ids=case_anchor_ids,
                    entities=CaseHoldingParaphraseEntities(
                        case_refs=case_refs,
                    ),
                ))
            else:
                candidates.append(ClaimCandidate(
                    claim_type=ClaimType.CASE_CITATION,
                    anchor_ids=[anchor.anchor],
                    entities=CaseCitationEntities(case_refs=case_refs),
                ))

        previous_anchor = anchor

    candidates.extend(
        candidate for anchor_id, candidate in deferred_table_sources.items()
        if anchor_id not in consumed_table_sources
    )
    return candidates


def _legal_claim_anchor_ids(anchor, block, parsed_doc, anchor_sources) -> list[str]:
    """同一段内，法条后的解释和结论属于同一主张，直到出现下一处引用。"""
    if block is None or anchor.anchor not in block.sentence_anchors:
        return [anchor.anchor]
    anchor_map = {item.anchor: item for item in parsed_doc.anchors}
    start = block.sentence_anchors.index(anchor.anchor)
    following: list[str] = []
    for anchor_id in block.sentence_anchors[start + 1:]:
        candidate = anchor_map.get(anchor_id)
        if (
            candidate is None
            or anchor_sources.get(anchor_id)
            or extract_case_refs(candidate.text)
            or has_article_reference(candidate.text)
        ):
            break
        following.append(anchor_id)
    return [anchor.anchor, *following]


def _case_holding_anchor_ids(anchor, block, parsed_doc, anchor_sources) -> list[str]:
    """把同段紧随案例名的事实与裁判规则归入同一条案例主张。"""
    if block is None or anchor.anchor not in block.sentence_anchors:
        return [anchor.anchor]
    anchor_map = {item.anchor: item for item in parsed_doc.anchors}
    start = block.sentence_anchors.index(anchor.anchor)
    following: list[str] = []
    has_holding = False
    for anchor_id in block.sentence_anchors[start + 1:start + 6]:
        candidate = anchor_map.get(anchor_id)
        if (
            candidate is None
            or anchor_sources.get(anchor_id)
            or extract_case_refs(candidate.text)
            or not _CASE_FOLLOWUP.match(candidate.text)
        ):
            break
        following.append(anchor_id)
        has_holding = has_holding or bool(_CASE_HOLDING_FOLLOWUP.search(candidate.text))
    return [anchor.anchor, *following] if has_holding else [anchor.anchor]


def _anchors_are_structurally_adjacent(previous, current, block_map: dict) -> bool:
    if previous.block_id == current.block_id:
        block = block_map.get(current.block_id)
        if block is None:
            return False
        try:
            return (
                block.sentence_anchors.index(current.anchor)
                == block.sentence_anchors.index(previous.anchor) + 1
            )
        except ValueError:
            return False
    block = block_map.get(current.block_id)
    if block is None:
        return False
    return any(
        relation.relation_type == BlockRelationType.PREVIOUS_BLOCK
        and relation.target_block_id == previous.block_id
        for relation in block.relations
    )


def _resolve_block_source(
    block, block_map: dict, records: dict, visited=None, current_anchor_id: str | None = None
):
    """沿 Block 关系解析唯一法源；说明块和歧义块终止链路。"""
    visited = set(visited or ())
    if block.block_id in visited:
        return None
    visited.add(block.block_id)
    own_records = records.get(block.block_id, [])
    if own_records and current_anchor_id in block.sentence_anchors:
        source_anchor_id, sources = own_records[-1]
        current_index = block.sentence_anchors.index(current_anchor_id)
        source_index = block.sentence_anchors.index(source_anchor_id)
        if current_index == source_index + 1 and len({source.title for source in sources}) == 1:
            return source_anchor_id, block, [sources[0]]
        if current_index != source_index + 1:
            return None
    relations = sorted(
        block.relations,
        key=lambda item: _RELATION_PRIORITY.get(item.relation_type, 99),
    )
    for relation in relations:
        target = block_map.get(relation.target_block_id)
        if not target:
            continue
        source_records = records.get(target.block_id, [])
        if source_records:
            flattened = [source for _, sources in source_records for source in sources]
            if len({source.title for source in flattened}) != 1:
                return None
            return source_records[0][0], target, [flattened[0]]
        # 只有纯条款 block 可继续承接；普通说明段立即截断。
        if has_article_reference(target.text):
            resolved = _resolve_block_source(target, block_map, records, visited)
            if resolved:
                return resolved
        if relation.relation_type != BlockRelationType.TABLE_ABOVE:
            return None
    return None


def _merge_partial_with_inherited_article(partial, source_list: list) -> list:
    """用唯一承前条号补齐仅款/项引用；歧义时放弃。"""
    from ..domain.citation import ArticleRef

    if partial is None or len(source_list) != 1:
        return []
    parent_articles = source_list[0].articles
    if len(parent_articles) != 1:
        return []
    parent = parent_articles[0]
    paragraphs = list(partial.paragraphs)
    if not paragraphs and partial.items:
        if len(parent.paragraphs) != 1:
            return []
        paragraphs = list(parent.paragraphs)
    return [ArticleRef(
        article=parent.article,
        paragraphs=paragraphs,
        items=list(partial.items),
    )]


def _build_inherited_sources(
    source_list: list,
    current_articles: list,
    source_anchor_id: str,
) -> list:
    """使用关系目标的唯一法源和当前条款构建继承实体。"""
    from ..domain.citation import ArticleRef, LegalSource

    inherited = []
    for source in source_list:
        inherited_from = source.recognition.inherited_from
        origin_anchor_id = (
            inherited_from.anchor_id
            if source.recognition.form == "inherited"
            and inherited_from is not None
            and inherited_from.anchor_id
            else source_anchor_id
        )
        copied_articles = [
            ArticleRef(
                article=article.article,
                paragraphs=list(article.paragraphs),
                items=list(article.items),
            )
            for article in current_articles
        ]
        inherited.append(LegalSource(
            title=source.title,
            canonical_title=source.canonical_title,
            raw_title_candidate=source.raw_title_candidate,
            raw_time=source.raw_time,
            articles=copied_articles,
            recognition=LegalSourceRecognition(
                form="inherited",
                resolver="context",
                inherited_from={"anchor_id": origin_anchor_id},
            ),
        ))
    return inherited


def _make_legal_candidate(
    anchor_id: str,
    legal_sources: list,
    alias_declarations: list[AliasDeclaration] | None = None,
) -> ClaimCandidate | None:
    if not legal_sources:
        return None
    return ClaimCandidate(
        claim_type=ClaimType.LEGAL_SOURCE_CLAIM,
        anchor_ids=[anchor_id],
        entities=LegalSourceClaimEntities(
            legal_sources=legal_sources,
            alias_declarations=alias_declarations or [],
        ),
    )
