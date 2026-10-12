const BOOKMARK_PREFIX = "_cc"
const SHORT_ANCHOR_LIMIT = 250
const LONG_ANCHOR_PIECE_LENGTH = 100

export async function seedSourceBookmarks(result) {
  assertWordApi()
  const targets = bookmarkTargets(result)
  const includeNotes = supportsWordApi15()

  return Word.run(async context => {
    const bookmarkNames = await getBookmarkNames(context, includeNotes)
    for (const name of bookmarkNames) {
      if (name.toLowerCase().startsWith(BOOKMARK_PREFIX)) {
        context.document.deleteBookmark(name)
      }
    }
    await context.sync()

    const searchable = []
    const failed = []
    for (const target of targets) {
      if (!includeNotes && isNoteBlock(target.location.block_id)) {
        failed.push(failureOf(target, "WordApi 1.4 不支持脚注或尾注定位"))
      } else {
        searchable.push(target)
      }
    }

    const located = await locateTargets(context, searchable, includeNotes)
    for (const target of searchable) {
      const match = located.get(target)
      if (!match) {
        failed.push(failureOf(target, "未找到 Anchor 原文"))
        continue
      }
      match.range.insertBookmark(target.bookmarkName)
    }
    await context.sync()
    return { failed }
  })
}

export async function jumpToSource(check, documentKey, locationIndex = 0) {
  assertWordApi()
  const target = locationIndex > 0
    ? targetAt(check, documentKey, locationIndex)
    : primaryTarget(check, documentKey)
  const includeNotes = supportsWordApi15()
  if (!includeNotes && isNoteBlock(target.location.block_id)) {
    throw new Error("WordApi 1.4 不支持定位脚注或尾注原文")
  }

  return Word.run(async context => {
    const bookmark = context.document.getBookmarkRangeOrNullObject(target.bookmarkName)
    bookmark.load("isNullObject")
    await context.sync()
    if (!bookmark.isNullObject) {
      bookmark.select()
      await context.sync()
      return { location: target.location, method: "bookmark" }
    }

    const located = await locateTargets(context, [target], includeNotes)
    const repaired = located.get(target)
    if (repaired) {
      repaired.range.insertBookmark(target.bookmarkName)
      repaired.range.select()
      await context.sync()
      return { location: target.location, method: "text_repair", search_method: repaired.method }
    }

    return selectBlockFallback(context, target.location, includeNotes)
  })
}

export function planSearchPieces(anchorText) {
  return rawSearchPieces(anchorText).map(escapeSearchText)
}

function rawSearchPieces(anchorText) {
  const segments = String(anchorText || "")
    .replace(/\r\n?/g, "\n")
    .split("\n")
    .map(segment => segment.trim())
    .filter(Boolean)
  const pieces = []
  for (const segment of segments) {
    if (segment.length <= SHORT_ANCHOR_LIMIT) {
      pieces.push(segment)
    } else {
      pieces.push(
        segment.slice(0, LONG_ANCHOR_PIECE_LENGTH),
        segment.slice(-LONG_ANCHOR_PIECE_LENGTH),
      )
    }
  }
  return pieces
}

export function escapeSearchText(text) {
  return String(text).replaceAll("^", "^^")
}

async function locateTargets(context, targets, includeNotes) {
  const scopes = await prepareSearchScopes(context, includeNotes, targets)
  const primary = queueSearchPass(scopes, targets, false)
  await loadSearchResults(context, primary)
  const located = resolveSearchPass(primary)
  await validateExpandedMatches(context, located)

  const fallbackTargets = targets.filter(target => {
    if (located.has(target) || isTableBlock(target.location.block_id)) return false
    const queued = primary.find(item => item.target === target)
    return queued?.scopeKind === "block"
  })
  if (fallbackTargets.length) {
    const fallback = queueSearchPass(scopes, fallbackTargets, true)
    await loadSearchResults(context, fallback)
    const fallbackLocated = resolveSearchPass(fallback)
    await validateExpandedMatches(context, fallbackLocated)
    for (const [target, match] of fallbackLocated) located.set(target, match)
  }
  return located
}

async function validateExpandedMatches(context, located) {
  const pending = []
  for (const [target, match] of located) {
    const pieces = rawSearchPieces(target.location.anchor_text)
    if (pieces.length <= 1) continue
    match.range.load("text")
    pending.push({ target, match, pieces })
  }
  if (!pending.length) return
  await context.sync()
  for (const { target, match, pieces } of pending) {
    if (!validateSearchSpan(match.range.text, pieces, target.location.anchor_text)) {
      located.delete(target)
    }
  }
}

export function validateSearchSpan(rangeText, pieces, anchorText) {
  const haystack = String(rangeText || "").replace(/\r\n?/g, "\n")
  let cursor = 0
  let firstPosition = null
  for (const piece of pieces) {
    const index = haystack.indexOf(piece, cursor)
    if (index < 0) return false
    if (firstPosition === null) firstPosition = index
    cursor = index + piece.length
  }
  const anchorLength = String(anchorText || "").replace(/\r\n?/g, "\n").length
  return cursor - firstPosition <= anchorLength + 32
}

async function prepareSearchScopes(context, includeNotes, targets) {
  const body = context.document.body
  const paragraphs = body.paragraphs
  paragraphs.load("items/tableNestingLevel")
  const tables = body.tables
  tables.load("items/rowCount,items/columnCount")
  const noteCollections = includeNotes
    ? [["footnote", body.footnotes], ["endnote", body.endnotes]]
    : []
  for (const [, collection] of noteCollections) collection.load("items")
  await context.sync()

  const ranges = new Map()
  let paragraphIndex = 0
  for (const paragraph of paragraphs.items) {
    if (paragraph.tableNestingLevel !== 0) continue
    ranges.set(`word:p:${paragraphIndex}`, paragraph.getRange())
    paragraphIndex += 1
  }
  for (const [type, collection] of noteCollections) {
    for (const note of collection.items) note.body.paragraphs.load("items")
    await context.sync()
    collection.items.forEach((note, noteIndex) => {
      note.body.paragraphs.items.forEach((paragraph, paragraphIndexInNote) => {
        ranges.set(`word:${type}:${noteIndex}:${paragraphIndexInNote}`, paragraph.getRange())
      })
    })
  }
  for (const target of targets) {
    const match = /^word:t:(\d+):(\d+):(\d+)$/.exec(target.location.block_id)
    if (!match) continue
    const table = tables.items[Number(match[1])]
    if (!table) continue
    const row = Number(match[2])
    const column = Number(match[3])
    if (row >= table.rowCount || column >= table.columnCount) continue
    ranges.set(
      target.location.block_id,
      table.getCell(row, column).body.getRange(),
    )
  }
  return { bodyRange: body.getRange(), ranges }
}

function queueSearchPass(scopes, targets, forceBody) {
  const queued = []
  for (const target of targets) {
    const location = target.location
    const blockRange = scopes.ranges.get(location.block_id)
    if (isTableBlock(location.block_id) && !blockRange) {
      queued.push({ target, collections: [], scopeKind: "cell", candidates: [] })
      continue
    }
    const useBody = forceBody || !blockRange
    const scope = useBody ? scopes.bodyRange : blockRange
    const scopeKind = useBody ? "full" : isTableBlock(location.block_id) ? "cell" : "block"
    const collections = planSearchPieces(location.anchor_text).map(piece => {
      const collection = scope.search(piece, searchOptions())
      collection.load("items")
      return collection
    })
    queued.push({ target, collections, scopeKind, candidates: [] })
  }
  return queued
}

async function loadSearchResults(context, queued) {
  if (!queued.length) return
  await context.sync()
  for (const item of queued) {
    item.candidates = item.collections.map(collection => collection.items.map(range => {
      if (!isTableBlock(item.target.location.block_id) || item.scopeKind === "cell") return { range, cell: null }
      const cell = range.parentTableCellOrNullObject
      cell.load("isNullObject,rowIndex,cellIndex")
      return { range, cell }
    }))
  }
  if (queued.some(item => item.candidates.some(group => group.some(candidate => candidate.cell)))) {
    await context.sync()
  }
}

function resolveSearchPass(queued) {
  const result = new Map()
  for (const item of queued) {
    if (!item.collections.length || item.candidates.some(group => !group.length)) continue
    const occurrence = Number.isInteger(item.target.location.occurrence)
      ? item.target.location.occurrence
      : 0
    const chosen = item.candidates.map(group => chooseCandidate(
      group,
      item.target.location,
      occurrence,
      item.scopeKind,
    ))
    if (chosen.some(candidate => !candidate)) continue
    const first = chosen[0].range
    const last = chosen.at(-1).range
    const range = chosen.length === 1 ? first : first.expandTo(last)
    result.set(item.target, {
      range,
      method: `${item.scopeKind}_search`,
    })
  }
  return result
}

function chooseCandidate(group, location, occurrence, scopeKind) {
  let valid = group
  if (isTableBlock(location.block_id) && scopeKind !== "cell") {
    valid = group.filter(({ cell }) => cell
      && !cell.isNullObject
      && cell.rowIndex === location.row_index
      && cell.cellIndex === location.cell_index)
    // Word Range 无法直接给出所属表格序号；同一行列出现多个候选时可能跨表，宁可降级。
    if (valid.length !== 1) return null
  } else if (scopeKind === "full" && group.length > 1) {
    // 段落索引已经漂移时无法可靠证明全文多命中的哪一个属于原块，宁可降级。
    return null
  }
  return valid[occurrence] || null
}

async function selectBlockFallback(context, location, includeNotes) {
  const paragraphMatch = /^word:p:(\d+)$/.exec(location.block_id)
  if (paragraphMatch) {
    const paragraphs = context.document.body.paragraphs
    paragraphs.load("items/tableNestingLevel")
    await context.sync()
    const topLevel = paragraphs.items.filter(item => item.tableNestingLevel === 0)
    const paragraph = topLevel[Number(paragraphMatch[1])]
    if (!paragraph) throw new Error("原文已删除，请重新核查")
    paragraph.getRange().select()
    await context.sync()
    return {
      location,
      method: "block_fallback",
      warning: "原句可能已被修改，已定位到所在段落",
    }
  }

  const tableMatch = /^word:t:(\d+):(\d+):(\d+)$/.exec(location.block_id)
  if (tableMatch) {
    try {
      const tables = context.document.body.tables
      tables.load("items")
      await context.sync()
      const table = tables.items[Number(tableMatch[1])]
      if (!table) throw new Error("table_missing")
      const range = table.getCell(Number(tableMatch[2]), Number(tableMatch[3])).body.getRange()
      range.select()
      await context.sync()
      return {
        location,
        method: "block_fallback",
        warning: "未能精确定位原句，已范围定位到所在单元格",
      }
    } catch {
      throw new Error("未能自动定位到原句，请手动查找")
    }
  }

  if (includeNotes && isNoteBlock(location.block_id)) {
    throw new Error("未能自动定位到原句，请手动查找")
  }
  throw new Error("原文已删除，请重新核查")
}

function searchOptions() {
  return {
    matchCase: false,
    matchWholeWord: false,
    matchWildcards: false,
    ignoreSpace: false,
    ignorePunct: false,
  }
}

function assertWordApi() {
  if (!window.Word?.run) throw new Error("当前 Word 版本不支持文档内定位")
}

function supportsWordApi15() {
  return Office.context.requirements.isSetSupported("WordApi", "1.5")
}

function bookmarkTargets(result) {
  const verification = result.verification
  const statuteCards = new Map()
  for (const check of verification.statute_results || []) {
    if (!statuteCards.has(check.card_id)) statuteCards.set(check.card_id, {
      card_id: check.card_id,
      source_locations: check.source_locations,
    })
  }
  const checks = [...statuteCards.values(), ...(verification.case_results || [])]
  return checks.flatMap(check => locationsForCheck(check, result.document_key))
}

function locationsForCheck(check, documentKey) {
  const checkId = check.card_id || check.check_id
  return (check.source_locations || [])
    .map((location, index) => ({
      checkId,
      index,
      location,
      bookmarkName: bookmarkName(documentKey, checkId, index),
    }))
    .filter(target => target.location.platform === "docx" && target.location.anchor_text)
}

function targetAt(check, documentKey, locationIndex) {
  const targets = locationsForCheck(check, documentKey)
  if (!targets.length) throw new Error("该结果缺少 Word 块定位信息")
  return targets[Math.min(locationIndex, targets.length - 1)]
}

function primaryTarget(check, documentKey) {
  const targets = locationsForCheck(check, documentKey)
  if (!targets.length) throw new Error("该结果缺少 Word 块定位信息")
  const first = targets[0]
  const sameTableRow = targets.length > 1
    && targets.every(target => target.location.table_index === first.location.table_index
      && target.location.row_index === first.location.row_index
      && target.location.table_index !== null)
  return sameTableRow ? targets.at(-1) : first
}

function bookmarkName(documentKey, checkId, index) {
  return `${BOOKMARK_PREFIX}${hashText(documentKey)}_${checkId.toLowerCase()}_${index}`
}

function hashText(value) {
  let hash = 2166136261
  for (const character of String(value)) {
    hash ^= character.codePointAt(0)
    hash = Math.imul(hash, 16777619)
  }
  return (hash >>> 0).toString(36)
}

function failureOf(target, reason) {
  return { check_id: target.checkId, location_index: target.index, reason }
}

function isNoteBlock(blockId) {
  return /^word:(footnote|endnote):/.test(blockId)
}

function isTableBlock(blockId) {
  return /^word:t:/.test(blockId)
}

async function getBookmarkNames(context, includeNotes) {
  const scopes = [context.document.body.getRange()]
  if (includeNotes) {
    const collections = [context.document.body.footnotes, context.document.body.endnotes]
    for (const collection of collections) collection.load("items")
    await context.sync()
    for (const collection of collections) {
      for (const note of collection.items) scopes.push(note.body.getRange())
    }
  }
  const results = scopes.map(scope => scope.getBookmarks(true, false))
  await context.sync()
  return [...new Set(results.flatMap(result => result.value))]
}
