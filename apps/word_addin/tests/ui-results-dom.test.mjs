import assert from "node:assert/strict"
import test from "node:test"

import { CheckUi, hasMachineFix } from "../assets/ui.js"

// ---------------------------------------------------------------------------
// 最小 DOM stub：ui.js 在渲染期只用到以下 API。
// 每个用例重新 setup()，互不污染。
// ---------------------------------------------------------------------------
class StubClassList {
  constructor(node) {
    this.node = node
    this.set = new Set()
  }
  syncFromString(value) {
    this.set = new Set(String(value).split(/\s+/).filter(Boolean))
  }
  toString() {
    return [...this.set].join(" ")
  }
  add(...names) {
    names.forEach(name => this.set.add(name))
    this.node._className = this.toString()
  }
  remove(...names) {
    names.forEach(name => this.set.delete(name))
    this.node._className = this.toString()
  }
  toggle(name, force) {
    const target = force === undefined ? !this.set.has(name) : force
    if (target) this.set.add(name)
    else this.set.delete(name)
    this.node._className = this.toString()
    return target
  }
  contains(name) {
    return this.set.has(name)
  }
}

class StubNode {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase()
    this.ownerDocument = ownerDocument
    this.childNodes = []
    this.attributes = {}
    this.dataset = {}
    this.listeners = {}
    this.hidden = false
    this.disabled = false
    this._text = ""
    this._className = ""
    this.classList = new StubClassList(this)
  }
  get className() { return this._className }
  set className(value) {
    this._className = String(value)
    this.classList.syncFromString(value)
  }
  set textContent(value) {
    this.childNodes = []
    this._text = String(value)
  }
  get textContent() {
    return this._text + this.childNodes.map(child => child.textContent ?? "").join("")
  }
  append(...nodes) {
    for (const node of nodes) {
      this.childNodes.push(typeof node === "string" ? this.ownerDocument.createTextNode(node) : node)
    }
  }
  appendChild(node) {
    this.childNodes.push(node)
    return node
  }
  replaceChildren(...nodes) {
    this.childNodes = []
    this.append(...nodes)
  }
  setAttribute(name, value) {
    this.attributes[name] = String(value)
  }
  set href(value) { this.attributes.href = String(value) }
  get href() { return this.attributes.href }
  set target(value) { this.attributes.target = String(value) }
  get target() { return this.attributes.target }
  set rel(value) { this.attributes.rel = String(value) }
  get rel() { return this.attributes.rel }
  getAttribute(name) {
    return this.attributes[name]
  }
  addEventListener(type, fn) {
    ;(this.listeners[type] ||= []).push(fn)
  }
  click() {
    for (const fn of this.listeners.click || []) fn({ target: this })
  }
}

class StubDocument {
  constructor() {
    this.byId = new Map()
  }
  createElement(tag) {
    return new StubNode(tag, this)
  }
  createTextNode(text) {
    return { nodeType: 3, textContent: String(text) }
  }
  getElementById(id) {
    return this.byId.get(id) || null
  }
  register(id) {
    const node = this.createElement("div")
    node.id = id
    this.byId.set(id, node)
    return node
  }
  querySelector() { return null }
  querySelectorAll() { return [] }
}

const SCREEN_IDS = [
  "home-screen", "progress-screen", "results-screen", "help-screen",
  "results-stats", "results-subtitle", "status-filter", "results-list",
  "message",
]

function setup() {
  const document = new StubDocument()
  for (const id of SCREEN_IDS) document.register(id)
  globalThis.document = document
  return document
}

function collect(node, predicate, out = []) {
  if (predicate(node)) out.push(node)
  for (const child of node.childNodes) {
    if (child.childNodes) collect(child, predicate, out)
  }
  return out
}

function byClass(root, className) {
  return collect(root, node => node.classList && node.classList.contains(className))
}

function textOf(node) {
  return node ? node.textContent : ""
}

// ---------------------------------------------------------------------------
// 数据夹具
// ---------------------------------------------------------------------------
let seq = 0
function statuteResult(overrides = {}) {
  seq += 1
  return {
    check_id: `vc_${seq}`,
    card_id: `card_${seq}`,
    display_group_id: `group_${seq}`,
    claim_id: `claim_${seq}`,
    claim_text: "当事人约定的违约金超过造成损失的百分之三十。",
    source_locations: [{ block_id: `word:p:${seq}` }],
    outcome: "pass",
    law_title: "中华人民共和国民法典",
    law_identity_resolved: true,
    article_no: "第585条",
    cited_locators: [{ article_no: "第585条" }],
    lookup_status: "article_found",
    findings: [],
    evidence: null,
    reference_role: "direct",
    jurisdiction: "CN",
    ...overrides,
  }
}

function issueFinding() {
  return {
    code: "article_number_error",
    risk_level: "HIGH",
    summary: "该版本中不存在第585条。",
    suggestion: "该版本中不存在第585条,建议改为第586条。",
    revision: {
      machine_applicable: true,
      strategy: "replace_exact_text",
      original_text: "第585条",
      revised_text: "第586条",
    },
  }
}

function issueEvidence() {
  return {
    law_title: "中华人民共和国民法典",
    article_no: "第586条",
    article_text: "约定的违约金低于造成的损失的,人民法院或者仲裁机构可以根据当事人的请求予以增加。",
    data_source: { source_url: "https://www.pkulaw.com/chl/586.html" },
  }
}

function caseResult(overrides = {}) {
  seq += 1
  return {
    check_id: `cc_${seq}`,
    display_group_id: `group_${seq}`,
    claim_id: `claim_${seq}`,
    claim_text: "在某案中,法院认为……",
    source_locations: [{ block_id: `word:p:${seq}` }],
    outcome: "bug",
    cited_case_name: "腾讯诉上海盈讯公司著作权侵权案",
    lookup_status: "manual_review",
    message: "北大法宝已返回相关候选,但无法确定唯一对应案例,请人工确认",
    ...overrides,
  }
}

function summary(overrides = {}) {
  return {
    total: 0, card_total: 0, reference_total: 0,
    passed: 0, issues: 0, reviews: 0, bugs: 0,
    ...overrides,
  }
}

function resultOf(statute_results = [], case_results = [], summaryOverrides = {}) {
  return {
    file_name: "测试文档.docx",
    document_key: "doc-key",
    verification: { schema_version: 1, statute_results, case_results },
    summary: summary(summaryOverrides),
  }
}

// ---------------------------------------------------------------------------
// 用例
// ---------------------------------------------------------------------------

test("review 与 bug 在统计行与筛选页签中合并为待核实", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [
      statuteResult({ outcome: "review", application_check: { reviews: [{ error_type: "meaning_distorted", suggestion: "请核查结论的独立法律依据。" }] } }),
      statuteResult({ outcome: "bug", lookup_status: "source_error", message: "模型服务不可用", source_attempts: [{ status: "source_error", source_name: "北大法宝 MCP" }] }),
    ],
    [],
    { total: 2, card_total: 2, reference_total: 2, reviews: 1, bugs: 1 },
  ))
  assert.match(textOf(document.getElementById("results-stats")), /共核查 2 条引用/)
  assert.match(textOf(document.getElementById("results-stats")), /0 处未通过/)
  assert.match(textOf(document.getElementById("results-stats")), /2 处待核实/)
  const tabs = byClass(document.getElementById("status-filter"), "status-tab")
  assert.equal(tabs.length, 4)
  assert.equal(tabs[0].getAttribute("aria-pressed"), "true")
  assert.equal(tabs[1].getAttribute("aria-pressed"), "false")
  tabs[1].click()
  assert.equal(tabs[0].getAttribute("aria-pressed"), "false")
  assert.equal(tabs[1].getAttribute("aria-pressed"), "true")
  assert.equal(byClass(document.getElementById("status-filter"), "status-tab")[1], tabs[1], "筛选后不应替换焦点按钮")
  assert.match(textOf(tabs[0]), /^全部2$/)
  assert.match(textOf(tabs[2]), /^待核实2$/)
  assert.match(textOf(tabs[3]), /^已通过0$/)
})

test("单条未通过默认展开:卡头静态、正文可见、不渲染 diff", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [statuteResult({ outcome: "issue", findings: [issueFinding()], evidence: issueEvidence() })],
    [],
    { total: 1, card_total: 1, reference_total: 1, issues: 1 },
  ))
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  assert.ok(card.classList.contains("is-issue"))
  const headers = byClass(card, "reference-item-header")
  assert.equal(headers.length, 1)
  assert.equal(headers[0].tagName, "DIV", "未通过卡头不应是按钮")
  assert.equal(byClass(card, "issue-tag").length, 0, "未通过卡头不应重复展示错误类型标签")
  const pills = byClass(card, "status-pill")
  assert.equal(pills.length, 1)
  assert.equal(textOf(pills[0]), "未通过")
  const body = byClass(card, "reference-item-body")[0]
  assert.equal(body.hidden, false)
  assert.match(textOf(card), /建议改为第586条/)
  assert.equal(byClass(card, "diff-remove").length, 0)
  assert.equal(byClass(card, "diff-add").length, 0)
  assert.ok(byClass(card, "authority-block").length, "权威来源应直接可见")
  assert.equal(byClass(card, "authority-link")[0].getAttribute("href"), "https://www.pkulaw.com/chl/586.html")
  assert.match(textOf(byClass(card, "authority-link")[0]), /查看权威原文/)
  assert.ok(byClass(card, "doc-quote").length, "文书原文块应在卡内")
})

test("仅有权威来源链接时不渲染空证据卡", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [statuteResult({
      outcome: "issue",
      findings: [issueFinding()],
      evidence: {
        law_title: "人工智能生成合成内容标识办法",
        article_no: "",
        article_text: "",
        data_source: { source_url: "https://www.miit.gov.cn/official.html" },
      },
    })],
    [],
    { total: 1, card_total: 1, reference_total: 1, issues: 1 },
  ))
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  assert.equal(byClass(card, "authority-block").length, 0)
  const link = byClass(card, "authority-link")[0]
  assert.equal(textOf(link), "查看权威来源 ↗")
  assert.equal(link.getAttribute("href"), "https://www.miit.gov.cn/official.html")
})

test("Ansvar 返回的 law.go.kr 链接渲染为查看权威原文", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [statuteResult({
      outcome: "issue",
      findings: [issueFinding()],
      evidence: {
        law_title: "저작권법",
        article_no: "제46조",
        article_text: "저작권법 제46조 본문……",
        data_source: { source_url: "https://www.law.go.kr/법령/저작권법" },
      },
    })],
    [],
    { total: 1, card_total: 1, reference_total: 1, issues: 1 },
  ))
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  assert.ok(byClass(card, "authority-block").length, "权威来源应直接可见")
  const link = byClass(card, "authority-link")[0]
  assert.match(textOf(link), /查看权威原文/)
  assert.equal(link.getAttribute("href"), "https://www.law.go.kr/법령/저작권법")
})

test("非白名单域名的来源链接仍被拦截", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [statuteResult({
      outcome: "issue",
      findings: [issueFinding()],
      evidence: {
        law_title: "某法规",
        article_no: "第一条",
        article_text: "第一条　条文内容……",
        data_source: { source_url: "https://evil.example.com/law" },
      },
    })],
    [],
    { total: 1, card_total: 1, reference_total: 1, issues: 1 },
  ))
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  assert.equal(byClass(card, "authority-link").length, 0, "非白名单域名不应渲染来源链接")
})

test("已通过默认收起为一行,点击卡头展开证据", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [statuteResult({
      outcome: "pass",
      evidence: { law_title: "中华人民共和国民法典", article_no: "第585条", article_text: "当事人可以约定一方违约时应当根据违约情况向对方支付一定数额的违约金。", data_source: {} },
    })],
    [],
    { total: 1, card_total: 1, reference_total: 1, passed: 1 },
  ))
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  assert.ok(card.classList.contains("is-pass"))
  const header = byClass(card, "reference-item-header")[0]
  assert.equal(header.tagName, "BUTTON")
  assert.equal(header.getAttribute("aria-expanded"), "false")
  const body = byClass(card, "reference-item-body")[0]
  assert.equal(body.hidden, true)
  assert.match(textOf(byClass(card, "status-pill")[0]), /已通过/)
  header.click()
  assert.equal(body.hidden, false)
  assert.equal(header.getAttribute("aria-expanded"), "true")
})

test("待核实默认展开且不重复显示灰色摘要", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [statuteResult({ outcome: "review", application_check: { reviews: [{ error_type: "meaning_distorted", suggestion: "所引条文不能直接推出该结论,请核查独立法律依据。" }] } })],
    [],
    { total: 1, card_total: 1, reference_total: 1, reviews: 1 },
  ))
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  assert.ok(card.classList.contains("is-pending"))
  const header = byClass(card, "reference-item-header")[0]
  assert.equal(header.tagName, "DIV")
  const body = byClass(card, "reference-item-body")[0]
  assert.equal(body.hidden, false)
  assert.equal(byClass(card, "pending-reason").length, 0)
  assert.match(textOf(body), /所引条文不能直接推出该结论/)
  assert.match(textOf(byClass(card, "status-pill")[0]), /待核实/)
})

test("一句多引聚合卡:共享一份文书原文,引用项无编号、细分割线、状态计数正确", () => {
  const document = setup()
  const ui = new CheckUi()
  const group = "group_multi"
  ui.renderResults(resultOf(
    [
      statuteResult({ display_group_id: group, outcome: "issue", findings: [issueFinding()], evidence: issueEvidence() }),
      statuteResult({ display_group_id: group, outcome: "review", application_check: { reviews: [{ error_type: "meaning_distorted", suggestion: "请核查结论依据。" }] } }),
      statuteResult({ display_group_id: group, outcome: "pass" }),
      statuteResult({ display_group_id: group, outcome: "pass", reference_role: "nested", relation_status: "resolved" }),
    ],
    [],
    { total: 4, card_total: 1, reference_total: 4, issues: 1, reviews: 1, passed: 2 },
  ))
  const cards = byClass(document.getElementById("results-list"), "result-card")
  assert.equal(cards.length, 1, "多引应聚合成一张外层卡")
  const card = cards[0]
  assert.ok(card.classList.contains("is-multiple"))
  assert.equal(byClass(card, "doc-quote-text").length, 1, "claim_text 只应出现一次")
  assert.match(textOf(byClass(card, "topline-label")[0]), /本段共 4 条引用/)
  const counts = textOf(byClass(card, "topline-counts")[0])
  assert.match(counts, /1 未通过/)
  assert.match(counts, /1 待核实/)
  assert.match(counts, /1 通过/)
  assert.match(counts, /1 内部转引/)
  const items = byClass(card, "reference-item")
  assert.equal(items.length, 4)
  const nested = items.find(item => item.classList.contains("is-nested"))
  assert.ok(nested, "内部转引行应有缩进标识类")
  assert.match(textOf(nested), /内部转引 · /)
})

test("carry_forward 引用带承前条款前缀", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [statuteResult({ outcome: "pass", reference_role: "carry_forward" })],
    [],
    { total: 1, card_total: 1, reference_total: 1, passed: 1 },
  ))
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  assert.match(textOf(card), /承前条款 · /)
})

test("machine_applicable 修订支持单条接受与取消", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [
      statuteResult({ outcome: "issue", findings: [issueFinding()], evidence: issueEvidence() }),
      statuteResult({ outcome: "issue", findings: [issueFinding()], evidence: issueEvidence() }),
    ],
    [],
    { total: 2, card_total: 2, reference_total: 2, issues: 2 },
  ))
  const firstDecisionButton = byClass(document.getElementById("results-list"), "decision-button")[0]
  const firstId = firstDecisionButton.dataset.checkId
  assert.equal(textOf(firstDecisionButton), "接受修订")
  ui.setDecision(firstId, "accepted")
  assert.equal(textOf(firstDecisionButton), "取消修订", "单条按钮应原地切换,不重建列表")
  ui.setDecision(firstId, null)
  assert.equal(textOf(firstDecisionButton), "接受修订")
})

test("缺少可执行替换内容的修订不显示接受入口", () => {
  assert.equal(hasMachineFix(statuteResult({
    findings: [{ revision: { machine_applicable: true } }],
  })), false)
})

test("案例结果字段不全时不报错,按待核实展开渲染", () => {
  const document = setup()
  const ui = new CheckUi()
  assert.doesNotThrow(() => {
    ui.renderResults(resultOf([], [caseResult()], { total: 1, card_total: 1, reference_total: 1, bugs: 1 }))
  })
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  assert.ok(card.classList.contains("is-pending"))
  const body = byClass(card, "reference-item-body")[0]
  assert.equal(body.hidden, false)
  assert.match(textOf(card), /腾讯诉上海盈讯公司著作权侵权案/)
  assert.equal(byClass(card, "pending-reason").length, 0)
  assert.match(textOf(body), /请人工确认/)
  assert.equal(byClass(card, "authority-block").length, 0, "无证据时不渲染权威来源块")
})

test("定位按钮:多处来源位置渲染多个定位入口", () => {
  const document = setup()
  const ui = new CheckUi()
  const jumps = []
  ui.setHandlers({ onJump: (target, index) => jumps.push(index) })
  ui.renderResults(resultOf(
    [statuteResult({ outcome: "issue", findings: [issueFinding()], evidence: issueEvidence(), source_locations: [{ block_id: "word:p:1" }, { block_id: "word:p:9" }] })],
    [],
    { total: 1, card_total: 1, reference_total: 1, issues: 1 },
  ))
  const buttons = byClass(document.getElementById("results-list"), "jump-button")
  assert.equal(buttons.length, 2)
  assert.match(textOf(buttons[0]), /定位 1/)
  assert.match(textOf(buttons[1]), /定位 2/)
  buttons[1].click()
  assert.deepEqual(jumps, [1])
})

test("定位按钮:同段重复与高度重叠位置只展示最完整的一处", () => {
  const document = setup()
  const ui = new CheckUi()
  const jumps = []
  ui.setHandlers({ onJump: (target, index) => jumps.push(index) })
  ui.renderResults(resultOf(
    [statuteResult({
      outcome: "issue", findings: [issueFinding()], evidence: issueEvidence(),
      source_locations: [
        { block_id: "word:p:1", char_start: 10, char_end: 40, anchor_text: "原文" },
        { block_id: "word:p:1", char_start: 10, char_end: 40, anchor_text: "原文" },
        { block_id: "word:p:1", char_start: 12, char_end: 38, anchor_text: "原文子串" },
        { block_id: "word:p:1", char_start: 8, char_end: 42, anchor_text: "最完整原文" },
      ],
    })],
    [],
    { total: 1, card_total: 1, reference_total: 1, issues: 1 },
  ))

  const buttons = byClass(document.getElementById("results-list"), "jump-button")
  assert.equal(buttons.length, 1)
  assert.equal(textOf(buttons[0]), "定位原文")
  buttons[0].click()
  assert.deepEqual(jumps, [3])
})

test("仅存在性核验通过时显示轻量来源链接而非完整权威卡片", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [statuteResult({
      cited_locators: [], article_no: "", lookup_status: "law_found_text_unavailable",
      evidence: {
        law_title: "中华人民共和国民法典",
        data_source: { source_url: "https://www.pkulaw.com/chl/123.html" },
      },
    })],
    [],
    { total: 1, card_total: 1, reference_total: 1, passed: 1 },
  ))
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  byClass(card, "reference-item-header")[0].click()

  assert.equal(byClass(card, "authority-block").length, 0)
  assert.match(textOf(byClass(card, "existence-status")[0]), /存在性核验通过/)
  const link = byClass(card, "existence-link")[0]
  assert.match(textOf(link), /查看《中华人民共和国民法典》权威来源/)
  assert.equal(link.getAttribute("href"), "https://www.pkulaw.com/chl/123.html")
})

test("条号错误不渲染绿色候选卡:对照留在权威来源块内,接受修订仍可用", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [statuteResult({
      outcome: "issue",
      findings: [issueFinding()],
      evidence: issueEvidence(),
      correction_evidence: {
        law_title: "中华人民共和国民法典",
        article_no: "第586条",
        article_text: "候选条文正文。",
        data_source: { source_url: "https://www.pkulaw.com/chl/586.html" },
      },
    })],
    [],
    { total: 1, card_total: 1, reference_total: 1, issues: 1 },
  ))
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  assert.equal(byClass(card, "candidate-citation").length, 0, "条号错误不应再渲染绿色候选卡")
  assert.equal(byClass(card, "reference-context-label").length, 0, "卡头不再标注原引用")
  const comparison = byClass(card, "authority-block")[0]
  assert.equal(comparison.classList.contains("is-comparison"), true)
  assert.match(textOf(byClass(card, "is-original")[0]), /原引用/)
  assert.match(textOf(byClass(card, "is-candidate")[0]), /候选修正引用/)

  const button = byClass(card, "decision-button")[0]
  assert.equal(textOf(button), "接受修订")
  ui.setDecision(button.dataset.checkId, "accepted")
  assert.equal(textOf(byClass(card, "decision-button")[0]), "取消修订")
})

test("非条号错误时绿色候选卡照常渲染", () => {
  const document = setup()
  const ui = new CheckUi()
  ui.renderResults(resultOf(
    [statuteResult({
      outcome: "issue",
      findings: [{ code: "format_error", risk_level: "LOW", summary: "引用编号格式不规范。", suggestion: "建议补齐编号。" }],
      candidate_citation: "《中华人民共和国民法典》第586条",
      evidence: issueEvidence(),
    })],
    [],
    { total: 1, card_total: 1, reference_total: 1, issues: 1 },
  ))
  const card = byClass(document.getElementById("results-list"), "result-card")[0]
  const candidate = byClass(card, "candidate-citation")[0]
  assert.match(textOf(candidate), /《中华人民共和国民法典》第586条/)
  assert.match(textOf(byClass(card, "reference-context-label")[0]), /原引用/)
})
