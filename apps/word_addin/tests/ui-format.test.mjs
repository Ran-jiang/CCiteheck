import assert from "node:assert/strict"
import test from "node:test"

import {
  CheckUi,
  stateMatchesFilter,
} from "../assets/ui.js"
import { orderChecksByCitation, sourceUrlOf, stripRepeatedArticleHeading } from "../assets/view-model.js"
import { formatReference, statuteErrorLabel, statuteViewOf } from "../assets/statute-view-model.js"
import { caseViewOf } from "../assets/case-view-model.js"

test("single-reference cards preserve card_id for bookmark lookup", () => {
  const ui = new CheckUi()
  ui.createResultCard = value => value
  const flattened = ui.createStatuteGroup({
    card_id: "card_00001",
    claim_text: "引用原文。",
    source_locations: [{ block_id: "word:p:0" }],
    references: [{ check_id: "vc_00001" }],
  })
  assert.equal(flattened.card_id, "card_00001")
  assert.equal(flattened.check_id, "vc_00001")
})

test("removes a repeated Chinese article heading when the card already has one", () => {
  const text = "第二百八十五条　违反国家规定，侵入计算机信息系统。\n第二款内容。"
  assert.equal(
    stripRepeatedArticleHeading(text, "第285条"),
    "违反国家规定，侵入计算机信息系统。\n第二款内容。"
  )
})

test("keeps article text untouched when no separate article number is shown", () => {
  const text = "第一条　第一条内容。\n第二条　第二条内容。"
  assert.equal(stripRepeatedArticleHeading(text, ""), text)
})

test("removes a repeated EUR-Lex Article heading", () => {
  assert.equal(stripRepeatedArticleHeading("Article 17\nRight to erasure.", "Article 17"), "Right to erasure.")
})

test("cards follow citation order instead of verification state", () => {
  const checks = [
    { check_id: "vc_3", anchor_ids: ["line00003"], status: "issue" },
    { check_id: "vc_1", anchor_ids: ["line00001"], status: "pass" },
    { check_id: "vc_2", anchor_ids: ["line00002"], status: "bug" },
  ]
  assert.deepEqual(
    orderChecksByCitation(checks).map((item) => item.check_id),
    ["vc_1", "vc_2", "vc_3"],
  )
})

test("pending filter keeps review and bug as separate internal states", () => {
  assert.equal(stateMatchesFilter("review", "pending"), true)
  assert.equal(stateMatchesFilter("bug", "pending"), true)
  assert.equal(stateMatchesFilter("issue", "pending"), false)
})

test("footnote cards sort by the body reference instead of the appended note block", () => {
  const checks = [
    { check_id: "vc_note", source_locations: [{ block_id: "word:footnote:2" }], sort_source_locations: [{ block_id: "word:p:2" }] },
    { check_id: "vc_body", source_locations: [{ block_id: "word:p:5" }] },
  ]
  assert.deepEqual(
    orderChecksByCitation(checks).map(item => item.check_id),
    ["vc_note", "vc_body"],
  )
})

test("hides obsolete lar links returned by the MCP law-list service", () => {
  const check = { evidence: { data_source: {
    source_url: "[北大法宝](https://www.pkulaw.com/lar/dead.html?way=mcp)",
  } } }
  assert.equal(sourceUrlOf(check), "")
})

test("keeps exact-article chl links", () => {
  const url = "https://pkulaw.com/chl/current.html"
  assert.equal(sourceUrlOf({ evidence: { data_source: { source_url: url } } }), url)
})

test("keeps Korean statute links from the Ansvar gateway", () => {
  const url = "https://www.law.go.kr/법령/저작권법"
  assert.equal(sourceUrlOf({ evidence: { data_source: { source_url: url } } }), url)
})

test("blocks source links from untrusted domains", () => {
  const check = { evidence: { data_source: { source_url: "https://evil.example.com/law" } } }
  assert.equal(sourceUrlOf(check), "")
})

test("formats one article with multiple paragraphs as one reference", () => {
  assert.equal(formatReference({
    law_title: "中华人民共和国商标法",
    article_no: "第十三条",
    paragraphs: ["第一款", "第三款"],
  }), "《中华人民共和国商标法》第十三条第一款、第三款")
})


test("unresolved bare law keeps raw text without invented book-title marks", () => {
  assert.equal(formatReference({
    recognition_form: "bare",
    law_identity_resolved: false,
    law_title: "依照城市房地产管理法",
    article_no: "第38条",
    paragraphs: [],
    items: [],
  }), "依照城市房地产管理法第38条")
})

test("citation error labels distinguish name, article and hierarchy", () => {
  assert.equal(statuteErrorLabel("law_name_error"), "法规名称错误")
  assert.equal(statuteErrorLabel("article_not_found"), "所引条文待核实")
  assert.equal(statuteErrorLabel("article_number_error"), "条文序号错误")
  assert.equal(statuteErrorLabel("citation_hierarchy_error"), "条款项层级错误")
  assert.equal(statuteErrorLabel("source_repealed"), "法源版本或效力错误")
  assert.equal(statuteErrorLabel("source_amended"), "法源版本或效力错误")
  assert.equal(statuteErrorLabel("future_error"), "future_error")
})

test("application labels are pure labels without a dimension prefix", () => {
  const view = statuteViewOf({
    outcome: "review",
    law_title: "民法典",
    lookup_status: "article_found",
    findings: [],
    application_check: {
      verdict: "review",
      reviews: [{
        error_type: "meaning_distorted",
        review_level: "待核查",
        summary: "所引条文不能直接推出该结论。",
        suggestion: "请核查结论的独立法律依据。",
      }],
    },
  })
  assert.deepEqual(view.typeTags, ["引文不忠实于权威原文"])
})

test("unknown application error types fall back to the raw code", () => {
  const view = statuteViewOf({
    outcome: "review",
    law_title: "民法典",
    lookup_status: "article_found",
    findings: [],
    application_check: {
      verdict: "review",
      reviews: [{
        error_type: "future_check_type",
        review_level: "待核查",
        summary: "新增检查类型。",
        suggestion: "请人工确认。",
      }],
    },
  })
  assert.deepEqual(view.typeTags, ["future_check_type"])
})

test("application suggestions use one separator without stacked punctuation", () => {
  const check = {
    outcome: "review",
    law_title: "民法典",
    lookup_status: "article_found",
    findings: [],
    application_check: {
      verdict: "review",
      reviews: [
        { error_type: "rule_fact_mismatch", suggestion: "请核对事实。" },
        { error_type: "meaning_distorted", suggestion: "请删除重复字；" },
      ],
    },
  }
  assert.equal(statuteViewOf(check).verdict.suggestion, "请核对事实；请删除重复字。")

  check.outcome = "issue"
  check.findings = [{ code: "article_number_error", risk_level: "HIGH", suggestion: "条号引用错误。" }]
  assert.doesNotMatch(statuteViewOf(check).verdict.suggestion, /。；/)
})

test("out-of-scope statutes surface the boundary message", () => {
  const view = statuteViewOf({
    law_title: "知识产权法典",
    outcome: "bug",
    lookup_status: "out_of_scope",
    source_attempts: [{ status: "out_of_scope", message: "涉外法规（非中国/欧盟法域），超出本产品核查边界，请人工核验" }],
  })
  assert.equal(view.state, "bug")
  assert.match(view.verdict.suggestion, /超出本产品核查边界/)
})

test("statute source failures surface the actionable provider message", () => {
  const view = statuteViewOf({
    law_title: "消防法",
    outcome: "bug",
    lookup_status: "source_error",
    message: "北大法宝鉴权失败（HTTP 401），请检查访问令牌、账户状态或剩余点数",
    source_attempts: [{ status: "source_error", source_name: "北大法宝 MCP" }],
  })
  assert.match(view.verdict.suggestion, /剩余点数/)
})

test("source-not-found labels use the actual MCP", () => {
  const view = statuteViewOf({
    law_title: "虚构条例",
    jurisdiction: "EU",
    outcome: "issue",
    lookup_status: "law_not_found",
    source_attempts: [{ status: "law_not_found", source_name: "EUR-Lex MCP" }],
    findings: [{ code: "source_not_found", risk_level: "HIGH", suggestion: "请核对。" }],
  })
  assert.deepEqual(view.typeTags, ["EUR-Lex MCP未检索到所引法源"])
})

test("EU statutes verified by EUR-Lex read as existence-only pass", () => {
  const view = statuteViewOf({
    law_title: "通用数据保护条例",
    jurisdiction: "EU",
    outcome: "pass",
    lookup_status: "relevant_articles_found",
    evidence: {
      law_title: "Regulation (EU) 2016/679",
      version_status: "现行有效",
      data_source: { source_url: "https://eur-lex.europa.eu/eli/reg/2016/679/oj" },
    },
  })
  assert.equal(view.state, "pass")
  assert.equal(view.reference, "《通用数据保护条例》")
  assert.equal(view.evidence.url, "https://eur-lex.europa.eu/eli/reg/2016/679/oj")
})

test("case checks normalize into the same shape as statutes", () => {
  const view = caseViewOf({
    check_kind: "case",
    outcome: "bug",
    check_id: "cc_00001",
    claim_text: "在某案中……",
    cited_case_name: "腾讯诉上海盈讯公司著作权侵权案",
    lookup_status: "manual_review",
    message: "北大法宝已返回相关候选，但无法确定唯一对应案例，请人工确认",
    evidence: { title: "某判决书", court: "深圳市南山区人民法院" },
  })
  assert.equal(view.state, "bug")
  assert.equal(view.reference, "腾讯诉上海盈讯公司著作权侵权案")
  assert.match(view.evidence.articleText, /深圳市南山区人民法院/)
})

test("finding card renders the self-contained suggestion without audit summary", () => {
  const view = statuteViewOf({
    outcome: "issue",
    law_title: "著作权司法解释",
    findings: [{
      code: "article_number_error",
      risk_level: "HIGH",
      summary: "第二十条规定的是出版者责任。",
      suggestion: "建议改引第十五条。",
    }],
  })
  assert.equal(view.verdict.suggestion, "建议改引第十五条。")
})

test("case candidates come from the explicit domain field", () => {
  const candidates = [{ title: "候选案例", case_number: "（2024）示例1号" }]
  const view = caseViewOf({
    outcome: "bug",
    check_id: "cc_1",
    cited_case_name: "某案",
    lookup_status: "manual_review",
    candidate_cases: candidates,
  })
  assert.deepEqual(view.candidates, candidates)
})

test("recalled related articles keep the evidence section even without full text", () => {
  const view = statuteViewOf({
    law_title: "著作权法",
    outcome: "pass",
    lookup_status: "relevant_articles_found",
    evidence: {
      law_title: "中华人民共和国著作权法",
      article_text: "第三条　本法所称的作品……",
      related_articles: [{ article_no: "第三条", article_text: "本法所称的作品……" }],
      data_source: {},
    },
  })
  assert.notEqual(view.evidence, null)
  assert.equal(view.evidence.articleText, "")
  assert.equal(view.evidence.related.length, 1)
})

test("normative paragraphs show their locator without inventing an article number", () => {
  const view = statuteViewOf({
    law_title: "某工作指导意见",
    outcome: "pass",
    lookup_status: "relevant_articles_found",
    evidence: {
      law_title: "某工作指导意见",
      related_articles: [{
        article_no: "",
        locator: "二、风险处置 · 段落2",
        locator_type: "paragraph",
        article_text: "建立重大风险报告机制。",
      }],
      data_source: {},
    },
  })
  assert.equal(view.evidence.related[0].heading, "二、风险处置 · 段落2")
})

test("EU evidence headings use the Article convention with a separator", () => {
  const view = statuteViewOf({
    law_title: "通用数据保护条例",
    article_no: "第十七条",
    jurisdiction: "EU",
    outcome: "pass",
    lookup_status: "article_found",
    application_check: { execution_status: "completed", verdict: "pass", reviews: [] },
    evidence: {
      law_title: "General Data Protection Regulation 2016/679",
      article_no: "Article 17",
      article_text: "Right to erasure…",
      data_source: { source_url: "https://eur-lex.europa.eu/x" },
    },
  })
  assert.equal(view.evidence.articleHeading, "General Data Protection Regulation 2016/679 · Article 17")
})

test("structure citations label distinctly from nested references", () => {
  const structure = statuteViewOf({
    law_title: "中华人民共和国民法典",
    outcome: "pass",
    article_no: "第三编第四章",
    lookup_status: "relevant_articles_found",
    evidence: {
      law_title: "中华人民共和国民法典",
      structure_path: "第三编 合同 / 第一分编 通则 / 第四章 合同的履行",
      related_articles: [{ article_no: "第五百零九条", article_text: "……" }],
      data_source: {},
    },
  })
  assert.equal(structure.state, "pass")
  assert.match(structure.evidence.structurePath, /合同的履行/)

  const ambiguous = statuteViewOf({
    law_title: "中华人民共和国民法典",
    outcome: "bug",
    article_no: "第四章",
    lookup_status: "relevant_articles_found",
    findings: [{
      code: "citation_hierarchy_error",
      risk_level: "MEDIUM",
      suggestion: "存在多个候选章节，请补充上级编号。",
    }],
    evidence: { law_title: "中华人民共和国民法典", structure_path: "候选：……", data_source: {} },
  })
  assert.equal(ambiguous.state, "bug")
  assert.deepEqual(ambiguous.typeTags, ["条款项层级错误"])
})

test("missing article copy depends on completeness verdict", () => {
  const base = { law_title: "示例法", outcome: "review", findings: [{ code: "article_not_found", risk_level: "MEDIUM" }] }
  assert.deepEqual(statuteViewOf(base).typeTags, ["所引条文待核实"])
  assert.deepEqual(statuteViewOf({ ...base, findings: [{ code: "article_not_found", risk_level: "HIGH" }] }).typeTags, ["该版本中不存在所引条号"])
  assert.equal(statuteErrorLabel("format_error"), "引用编号格式错误")
})

test("correction evidence is shown alongside original evidence", () => {
  const view = statuteViewOf({ law_title: "示例法", outcome: "issue",
    evidence: { article_no: "第一条", article_text: "第一条　原始条文" },
    correction_evidence: { article_no: "第二条", article_text: "第二条\n候选条文", data_source: { source_url: "https://pkulaw.com/chl/test.html" } },
  })
  assert.deepEqual(view.evidence.related.map(item => item.text), ["原始条文", "候选条文"])
  assert.deepEqual(view.evidence.related.map(item => item.tone), ["original", "candidate"])
  assert.equal(view.evidence.comparison, true)
  assert.equal(view.evidence.url, "https://pkulaw.com/chl/test.html")
})


test("verified lawinfochina criminal-law version link is visible", () => {
  const url = "https://www.lawinfochina.com/display.aspx?id=34470&lib=law"
  assert.equal(sourceUrlOf({ evidence: { data_source: { source_url: url } } }), url)
})
