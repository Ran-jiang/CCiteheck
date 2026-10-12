import { sourceUrlOf, sourceUrlsOf, stripRepeatedArticleHeading } from "./view-model.js"

const STATUTE_ERROR_LABELS = {
  source_not_found: "MCP未检索到所引法源",
  law_name_error: "法规名称错误",
  article_not_found: "所引条文待核实",
  format_error: "引用编号格式错误",
  article_number_error: "条文序号错误",
  citation_hierarchy_error: "条款项层级错误",
  source_repealed: "法源版本或效力错误",
  source_amended: "法源版本或效力错误",
}

const APPLICATION_ERROR_LABELS = {
  rule_fact_mismatch: "法条与事实关联性弱",
  meaning_distorted: "引文不忠实于权威原文",
}

export function formatReference(check) {
  if (check.law_identity_resolved === false) {
    return `${check.law_title || "未确定法名"}${check.article_no || ""}${(check.paragraphs || []).join("、")}${(check.items || []).join("、")}`
  }
  return `《${check.law_title}》${check.article_no || ""}${(check.paragraphs || []).join("、")}${(check.items || []).join("、")}`
}

export function statuteViewOf(check) {
  const state = check.outcome || "bug"
  const findings = check.findings || []
  const applicationReviews = check.application_check?.reviews || []
  return {
    checkId: check.check_id, state,
    reference: formatReference(check),
    verdict: statuteVerdict(check, state, findings, applicationReviews), evidence: statuteEvidence(check),
    verificationMode: verificationModeOf(check),
    authoritySources: sourceUrlsOf(check),
    candidateCitation: candidateCitationOf(check),
    typeTags: [
      ...new Set([
        ...findings.map(finding => statuteErrorLabel(finding.code, check)),
        ...applicationReviews.map(review => APPLICATION_ERROR_LABELS[review.error_type] || review.error_type),
      ])
    ],
    raw: check,
  }
}

export function verificationModeOf(check) {
  const explicit = check.verification_mode || check.verificationMode
  if (explicit === "existence_only") return "existence"
  if (["existence", "content"].includes(explicit)) return explicit
  const hasLocator = (check.cited_locators || []).some(locator =>
    locator.article_no || locator.paragraph_no || locator.item_no)
  return check.outcome === "pass" && !hasLocator && !check.application_check ? "existence" : "content"
}

function candidateCitationOf(check) {
  // 条号错误时权威来源块内已有"原引用/候选修正引用"对照,不再单独渲染绿色候选卡。
  if ((check.findings || []).some(finding => finding.code === "article_number_error")) return null
  let candidate = check.candidate_citation || check.candidateCitation
    || check.suggested_citation || check.suggestedCitation
    || check.corrected_citation || check.correctedCitation || null
  if (Array.isArray(candidate)) candidate = candidate[0]
  if (typeof candidate === "string") return { text: candidate }
  const resolved = (check.findings || []).find(finding => finding.resolved_locator)?.resolved_locator
  const evidence = check.correction_evidence
  if (!candidate && !resolved && !evidence) return null
  candidate ||= {}
  const locator = candidate.locator || candidate.cited_locator || candidate.cited_locators?.[0] || resolved || {}
  const directText = candidate.display_text || candidate.displayText || candidate.citation_text || candidate.citationText || candidate.text
  if (directText) return { text: directText }
  const lawTitle = candidate.law_title || candidate.lawTitle || candidate.title || evidence?.law_title || check.law_title
  const articleNo = candidate.article_no || candidate.articleNo || locator.article_no || evidence?.article_no || ""
  const paragraphs = [].concat(candidate.paragraphs || locator.paragraph_no || []).filter(Boolean)
  const items = [].concat(candidate.items || locator.item_no || []).filter(Boolean)
  const text = `${lawTitle ? `《${lawTitle}》` : ""}${articleNo}${paragraphs.join("、")}${items.join("、")}`
  return text ? { text } : null
}

function statuteVerdict(check, state, findings, applicationReviews) {
  if (findings.length) {
    const first = findings[0]
    const applicationText = applicationReviewText(applicationReviews)
    return {
      suggestion: [findingText(first), applicationText].filter(Boolean).join("\n"),
    }
  }
  if (applicationReviews.length) {
    return {
      suggestion: applicationReviewText(applicationReviews),
    }
  }
  if (check.lookup_status === "out_of_scope") {
    const message = (check.source_attempts || []).find(item => item.status === "out_of_scope")?.message
    return message ? { suggestion: message } : null
  }
  if (check.reference_role === "nested" && check.relation_message) {
    return { suggestion: check.relation_message }
  }
  if ((state === "bug" || state === "review") && check.message) {
    return { suggestion: check.message }
  }
  return null
}

function applicationReviewText(reviews) {
  const text = reviews
    .map(review => review.suggestion || review.summary)
    .filter(Boolean)
    .map(value => String(value).trim().replace(/[。；]+$/, ""))
    .filter(Boolean)
    .join("；")
  return text ? `${text}。` : ""
}

export function statuteErrorLabel(code, check = {}) {
  if (code === "article_not_found" && check.findings?.some(f => f.code === code && f.risk_level === "HIGH")) return "该版本中不存在所引条号"
  const label = STATUTE_ERROR_LABELS[code] || code
  return code === "source_not_found" ? `${sourceNameOf(check)}未检索到所引法源` : label
}

function sourceNameOf(check) {
  const attempts = check.source_attempts || []
  const trace = [...attempts].reverse().find(item => item.status === "law_not_found")
    || check.evidence?.data_source
  const fallback = check.jurisdiction === "EU"
    ? "EUR-Lex MCP"
    : check.jurisdiction === "CN" ? "北大法宝 MCP" : "Ansvar Gateway"
  const name = String(trace?.source_name || fallback).trim()
  return /\bMCP$/i.test(name) ? name : `${name} MCP`
}

function statuteEvidence(check) {
  const evidence = check.correction_evidence || check.evidence
  const url = sourceUrlOf(check.correction_evidence
    ? { ...check, evidence: check.correction_evidence, correction_evidence: null }
    : check)
  const related = (evidence?.related_articles || []).map(item => ({ heading: item.locator || item.article_no || "", text: item.article_text || "" }))
  let comparison = false
  if (check.correction_evidence?.article_text && check.evidence?.article_text && !related.length) {
    comparison = true
    related.push(
      { tone: "original", heading: `原引用 · ${check.evidence.article_no || ""}`, text: stripRepeatedArticleHeading(check.evidence.article_text, check.evidence.article_no) },
      { tone: "candidate", heading: `候选修正引用 · ${check.correction_evidence.article_no || ""}`, text: stripRepeatedArticleHeading(check.correction_evidence.article_text, check.correction_evidence.article_no) },
    )
  }
  // related_articles 是 article_text 聚合内容的逐条结构化版本；两者只能展示
  // 一种，否则同一色块会先显示整组法条，再逐条重复一次。
  const articleText = related.length ? "" : stripRepeatedArticleHeading(evidence?.article_text, check.article_no)
  const structurePath = evidence?.structure_path || ""
  if (!articleText && !related.length && !url && !structurePath) return null
  const lawTitle = evidence?.law_title || check.law_title
  const articleNo = evidence?.article_no || check.article_no || ""
  const heading = !articleNo ? lawTitle : /^第/.test(articleNo) ? `${lawTitle}${articleNo}` : `${lawTitle} · ${articleNo}`
  return { articleHeading: articleText ? heading : "", articleText, related, url, structurePath, comparison }
}

function findingText(finding) {
  const summary = String(finding.summary || "").trim().replace(/[。；]+$/, "")
  const suggestion = String(finding.suggestion || "").trim()
  return suggestion || (summary ? `${summary}。` : "")
}
