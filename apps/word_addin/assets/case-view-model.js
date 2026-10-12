import { sourceUrlOf } from "./view-model.js"

const CASE_ERROR_LABELS = {
  case_not_found: "北大法宝未检索到引用案例", case_identity_error: "案例引用信息错误",
  holding_unsupported: "所述观点在裁判说理中无对应依据",
  holding_distorted: "曲解裁判说理原意",
}

export function caseViewOf(check) {
  const state = check.outcome || "bug"
  const findings = check.findings || []
  const first = findings[0]
  const verdict = first
    ? { suggestion: findingText(first) }
    : check.message ? { suggestion: check.message } : null
  const line = check.evidence
    ? `${check.evidence.title || check.evidence.case_number || ""}${check.evidence.court ? ` · ${check.evidence.court}` : ""}`
    : ""
  return {
    checkId: check.check_id, state,
    reference: check.cited_case_number || check.cited_case_name || "未命名案例线索",
    verdict,
    evidence: check.evidence ? { articleHeading: "", articleText: line, related: [], url: sourceUrlOf(check) } : null,
    candidates: check.candidate_cases || [],
    typeTags: findings.map(finding => CASE_ERROR_LABELS[finding.code] || finding.code),
    raw: check,
  }
}

function findingText(finding) {
  const summary = String(finding.summary || "").trim().replace(/[。；]+$/, "")
  const suggestion = String(finding.suggestion || "").trim()
  const base = suggestion || (summary ? `${summary}。` : "")
  const excerpt = String(finding.matched_excerpt || "").trim()
  return excerpt ? `${base}\n裁判说理原文：${excerpt}` : base
}
