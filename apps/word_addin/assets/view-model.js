// 结果页共享视觉工具；不解释法规或案例业务状态。

export function sourceUrlOf(check) {
  return sourceUrlsOf(check)[0]?.url || ""
}

export function sourceUrlsOf(check) {
  const sources = [
    [check.evidence?.law_title || check.law_title, check.evidence?.data_source?.source_url],
    [check.evidence?.law_title || check.law_title, check.evidence?.url],
    ...(check.evidence?.related_articles || []).map(item => [item.law_title || check.evidence?.law_title || check.law_title, item.source_url]),
    [check.correction_evidence?.law_title || check.law_title, check.correction_evidence?.data_source?.source_url],
  ]
  const seen = new Set()
  return sources.flatMap(([title, raw]) => {
    const url = trustedSourceUrl(raw)
    if (!url || seen.has(url)) return []
    seen.add(url)
    return [{ title: title || "权威来源", url }]
  })
}

function trustedSourceUrl(raw) {
  const match = String(raw).match(/\((https?:\/\/[^)]+)\)/)
  const url = match ? match[1] : String(raw).startsWith("http") ? String(raw) : ""
  try {
    const parsed = new URL(url)
    const legacy = parsed.pathname.startsWith("/lar/") && parsed.searchParams.get("way") === "mcp"
    const trusted = /(^|\.)pkulaw\.com$/i.test(parsed.hostname) || /(^|\.)europa\.eu$/i.test(parsed.hostname) || /(^|\.)lawinfochina\.com$/i.test(parsed.hostname) || /(^|\.)law\.go\.kr$/i.test(parsed.hostname) || /\.gov\.cn$/i.test(parsed.hostname)
    return trusted && !legacy ? url : ""
  } catch {
    return ""
  }
}

export function stripRepeatedArticleHeading(text, articleNo) {
  if (!articleNo) return String(text || "")
  return String(text || "").replace(
    /^\s*(?:第[〇零一二三四五六七八九十百千万两0-9]+条(?:之[〇零一二三四五六七八九十百千万两0-9]+)?|Article\s+\d+[A-Za-z]?)[\s　]*/i,
    "",
  )
}

export function orderChecksByCitation(checks) {
  const anchor = check => Number(String(
    check.sort_source_locations?.[0]?.block_id || check.source_locations?.[0]?.block_id || ""
  ).replace(/\D/g, "")) || Number.MAX_SAFE_INTEGER
  return [...checks].sort((left, right) => {
    const leftId = left.card_id || left.check_id
    const rightId = right.card_id || right.check_id
    return anchor(left) - anchor(right) || leftId.localeCompare(rightId)
  })
}
