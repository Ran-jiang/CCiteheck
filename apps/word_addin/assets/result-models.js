export function buildResultCards(verification) {
  const cards = new Map()
  for (const result of verification.statute_results || []) {
    addResult(cards, result, "statute")
  }
  for (const result of verification.case_results || []) {
    addResult(cards, result, "case")
  }
  return [...cards.values()].map(card =>
    card.references.length === 1 && card.references[0].check_kind === "case"
      ? card.references[0]
      : card
  )
}

function addResult(cards, result, kind) {
  if (!result.display_group_id) throw new Error(`${kind === "case" ? "案例" : "法规"}核验结果缺少 display_group_id`)
  const groupId = result.display_group_id
  const sourceLocations = result.note_context?.referenced_from || result.source_locations
  const card = cards.get(groupId) || {
    card_id: kind === "case" ? result.check_id : result.card_id,
    claim_text: result.claim_text,
    source_locations: sourceLocations,
    display_locations: displayLocationsOf(sourceLocations),
    sort_source_locations: sourceLocations,
    note_context: result.note_context || null,
    references: [],
    check_kind: "statute-group",
  }
  if (result.note_context) {
    card.note_context = result.note_context
    card.sort_source_locations = result.note_context.referenced_from || card.sort_source_locations
  }
  card.references.push(kind === "case"
    ? { ...result, check_kind: "case", display_locations: displayLocationsOf(result.source_locations) }
    : normalizeStatuteResult(result))
  cards.set(groupId, card)
}

function normalizeStatuteResult(result) {
  const locator = result.cited_locators?.[0] || {}
  return {
    ...result,
    check_kind: "statute",
    display_locations: displayLocationsOf(result.source_locations),
    article_no: locator.article_no,
    paragraphs: result.cited_locators?.map(item => item.paragraph_no).filter(Boolean) || [],
    items: result.cited_locators?.map(item => item.item_no).filter(Boolean) || [],
  }
}

export function displayLocationsOf(locations = []) {
  const groups = []
  locations.forEach((location, sourceIndex) => {
    if (!location) return
    const candidate = { ...location, source_location_index: sourceIndex }
    const group = groups.find(current => relatedLocation(current, candidate))
    if (!group) {
      groups.push({ representative: candidate, start: candidate.char_start, end: candidate.char_end })
      return
    }
    if ((sameLocation(group.representative, candidate) || highlyOverlaps(group.representative, candidate))
      && locationScore(candidate) > locationScore(group.representative)) {
      group.representative = candidate
    }
    if (hasSpan(candidate)) {
      group.start = Math.min(group.start, candidate.char_start)
      group.end = Math.max(group.end, candidate.char_end)
    }
  })
  return groups.map(group => group.representative)
}

function relatedLocation(group, candidate) {
  const current = group.representative
  if (sameLocation(current, candidate) || highlyOverlaps(current, candidate)) return true
  if (!sameScope(current, candidate) || !hasSpan(candidate) || !Number.isFinite(group.end)) return false
  return candidate.char_start === group.end || candidate.char_end === group.start
}

function sameLocation(left, right) {
  if (!sameScope(left, right)) return false
  if (hasCoordinates(left) && hasCoordinates(right)) return left.char_start === right.char_start && left.char_end === right.char_end
  return left.anchor_text === right.anchor_text && left.occurrence === right.occurrence
}

function highlyOverlaps(left, right) {
  if (!sameScope(left, right) || !hasSpan(left) || !hasSpan(right)) return false
  const overlap = Math.min(left.char_end, right.char_end) - Math.max(left.char_start, right.char_start)
  const shorter = Math.min(left.char_end - left.char_start, right.char_end - right.char_start)
  return overlap > 0 && overlap / shorter >= .8
}

function sameScope(left, right) {
  return ["platform", "document_id", "revision", "block_id", "table_index", "row_index", "cell_index"]
    .every(field => (left[field] ?? null) === (right[field] ?? null))
}

function hasSpan(location) {
  return hasCoordinates(location) && location.char_end > location.char_start
}

function hasCoordinates(location) {
  return Number.isFinite(location.char_start) && Number.isFinite(location.char_end)
}

function locationScore(location) {
  return (hasSpan(location) ? location.char_end - location.char_start : 0) * 1000
    + String(location.anchor_text || "").length
    + (Number.isInteger(location.occurrence) ? 1 : 0)
}
