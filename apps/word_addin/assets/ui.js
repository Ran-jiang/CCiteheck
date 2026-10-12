import {
  orderChecksByCitation,
} from "./view-model.js"
import { caseViewOf } from "./case-view-model.js"
import { statuteViewOf } from "./statute-view-model.js"
import { buildResultCards } from "./result-models.js"
import { revisionFor } from "./word-revisions.js"

const screens = ["home-screen", "progress-screen", "results-screen", "help-screen"]

const REFERENCE_ROLE_LABELS = { nested: "内部转引", carry_forward: "承前条款" }

// UI 展示层的三类业务状态:底层 review / bug 合并为"待核实",数据层不变。
export function displayState(state) {
  return ["review", "bug"].includes(state) ? "pending" : state
}

const STATUS_PILL_TEXT = { pass: "已通过", issue: "未通过", review: "待核实", bug: "待核实" }

export function hasMachineFix(check) {
  return Boolean(revisionFor(check))
}

function statNumber(value) {
  return element("span", "stat-number", String(value))
}

function viewOf(check) {
  return check.check_kind === "case" ? caseViewOf(check) : statuteViewOf(check)
}

export function stateMatchesFilter(state, filter) {
  return filter === "all"
    || state === filter
    || (filter === "pending" && ["review", "bug"].includes(state))
}

export class CheckUi {
  constructor() {
    this.messageTimer = null
    this.handlers = { onJump: null, onHistoryOpen: null }
    this.decisions = {}
    this.decisionSyncers = new Map()
    this.cards = []
    this.ready = false
    this.statusFilter = "all"
  }

  setHandlers(handlers) {
    this.handlers = { ...this.handlers, ...handlers }
  }

  showScreen(id) {
    for (const screenId of screens) {
      document.getElementById(screenId).classList.toggle("is-hidden", screenId !== id)
    }
  }

  setDocument(name, status, ready) {
    this.ready = ready
    document.getElementById("document-name").textContent = name
    document.getElementById("document-status").textContent = status
    document.getElementById("connection-dot").classList.toggle("is-ready", ready)
    document.getElementById("start-button").disabled = !ready
    document.getElementById("selection-button").disabled = !ready
  }

  setBusy(busy) {
    for (const id of ["start-button", "selection-button"]) {
      const button = document.getElementById(id)
      if (button) button.disabled = busy || !this.ready
    }
    for (const id of ["rerun-button", "export-button"]) {
      const button = document.getElementById(id)
      if (button) button.disabled = busy
    }
    document.querySelector(".app-shell").setAttribute("aria-busy", String(busy))
  }

  setLocateFailures(failures) {
    const failedIds = new Set(failures.map(item => item.check_id))
    for (const button of document.querySelectorAll(".jump-button[data-locate-id]")) {
      const failed = failedIds.has(button.dataset.locateId)
      button.classList.toggle("has-warning", failed)
      button.title = failed ? "定位标记未创建，点击时将重新匹配原文" : ""
    }
  }

  setStage(id, state, detail) {
    const stage = document.getElementById(id)
    stage.classList.remove("is-active", "is-complete")
    if (state) stage.classList.add(`is-${state}`)
    if (detail) stage.querySelector("small").textContent = detail
  }

  resetProgress() {
    this.setStage("stage-read", "active", "正在获取完整 DOCX")
    this.setStage("stage-submit", "", "等待执行")
    this.setStage("stage-check", "", "等待执行")
  }

  renderHistory(history) {
    const section = document.getElementById("history-section")
    const list = document.getElementById("history-list")
    if (!section || !list) return
    section.classList.toggle("is-hidden", !history.length)
    list.replaceChildren(...history.map(entry => {
      const row = element("button", "history-row")
      row.type = "button"
      row.addEventListener("click", () => this.handlers.onHistoryOpen?.(entry))
      const copy = element("div", "history-copy")
      copy.append(
        element("div", "history-name", entry.fileName || "未命名文档"),
        element("div", "history-meta", `${formatCheckedAt(entry.checkedAt)} · 共 ${entry.total} 处引用`)
      )
      const badge = entry.issues > 0
        ? element("span", "history-badge is-issue", `${entry.issues} 处未通过`)
        : element("span", "history-badge is-pass", "全部通过")
      row.append(copy, badge)
      return row
    }))
  }

  renderResults(result, decisions = {}, options = {}) {
    const { summary, verification } = result
    this.decisions = decisions
    this.decisionSyncers = new Map()
    this.cards = orderChecksByCitation(buildResultCards(verification))
    const pending = (summary.reviews || 0) + (summary.bugs || 0)
    document.getElementById("results-stats").replaceChildren(
      "共核查 ", statNumber(summary.reference_total), " 条引用，",
      statNumber(summary.issues), " 处未通过，",
      statNumber(pending), " 处待核实",
    )
    document.getElementById("results-subtitle").textContent = options.snapshotAt
      ? `${result.file_name} · ${formatCheckedAt(options.snapshotAt)} 的核查快照`
      : result.file_name
    this.statusFilter = "all"
    this.renderStatusFilter(summary)
    this.renderChecks()
    this.showScreen("results-screen")
  }

  renderStatusFilter(summary) {
    const container = document.getElementById("status-filter")
    const pending = (summary.reviews || 0) + (summary.bugs || 0)
    const options = [
      ["all", "全部", summary.reference_total],
      ["issue", "未通过", summary.issues],
      ["pending", "待核实", pending],
      ["pass", "已通过", summary.passed],
    ]
    container.replaceChildren()
    const buttons = []
    for (const [value, label, count] of options) {
      const button = element("button", `status-tab${value === this.statusFilter ? " is-active" : ""}`)
      button.type = "button"
      button.setAttribute("aria-pressed", String(value === this.statusFilter))
      button.append(label, element("em", "status-count", String(count)))
      button.addEventListener("click", () => {
        this.statusFilter = value
        for (const [index, item] of buttons.entries()) {
          const active = options[index][0] === value
          item.classList.toggle("is-active", active)
          item.setAttribute("aria-pressed", String(active))
        }
        this.renderChecks()
      })
      buttons.push(button)
      container.append(button)
    }
  }

  renderChecks() {
    const list = document.getElementById("results-list")
    list.replaceChildren()
    const visible = this.cards.filter(item => {
      const checks = item.check_kind === "statute-group" ? item.references : [item]
      return checks.some(check => stateMatchesFilter(check.outcome, this.statusFilter))
    })
    if (!visible.length) {
      list.append(element("div", "empty-results", "该状态下没有核查结果。"))
      return
    }
    for (const item of visible) {
      list.append(item.check_kind === "statute-group" ? this.createStatuteGroup(item) : this.createResultCard(item))
    }
  }

  createStatuteGroup(card) {
    if (card.references.length === 1) {
      return this.createResultCard({
        ...card.references[0],
        card_id: card.card_id,
        claim_text: card.claim_text,
        source_locations: card.source_locations,
        display_locations: card.display_locations,
        note_context: card.note_context || null,
      })
    }
    return this.createMultiReferenceCard(card)
  }

  // 单条引用卡:卡头(条名/徽章) → 文书原文 → 建议/diff →
  // 权威来源 → 接受修订。未通过/待核实默认全部展开;
  // 已通过收起为一行,点击卡头展开引文与权威来源。
  createResultCard(check) {
    const view = viewOf(check)
    const state = displayState(view.state)
    const card = element("article", `result-card is-${state}${check.note_context ? " is-footnote" : ""}`)
    const body = this.createItemBody(view, { includeQuote: state === "pass" })
    card.append(this.createItemHeader(view, state === "pass", body))
    if (state !== "pass") {
      card.append(this.createQuoteZone(check.claim_text, check, check.card_id || check.check_id, check.note_context))
    }
    body.hidden = state === "pass"
    card.append(body)
    return card
  }

  // 一句多引聚合卡:外层一张卡,共享文书原文,引用项之间细分割线、无嵌套边框。
  createMultiReferenceCard(card) {
    const views = card.references.map(viewOf)
    const container = element("article", `result-card is-multiple${card.note_context ? " is-footnote" : ""}`)

    const top = element("div", "result-topline")
    top.append(element("span", "topline-label", `${card.note_context ? "脚注 · " : ""}本段共 ${views.length} 条引用`))
    const counts = element("span", "topline-counts")
    const primaryViews = views.filter(view => view.raw.reference_role !== "nested")
    const nestedCount = views.length - primaryViews.length
    const countOf = state => primaryViews.filter(view => displayState(view.state) === state).length
    const issueCount = countOf("issue")
    const pendingCount = countOf("pending")
    const passCount = countOf("pass")
    if (issueCount) counts.append(element("span", "count-issue", `${issueCount} 未通过`))
    if (pendingCount) counts.append(element("span", "count-pending", `${pendingCount} 待核实`))
    if (passCount) counts.append(element("span", "count-pass", `${passCount} 通过`))
    if (nestedCount) counts.append(element("span", "count-nested", `${nestedCount} 内部转引`))
    top.append(counts)
    container.append(top)

    container.append(this.createQuoteZone(card.claim_text, card, card.card_id, card.note_context))

    const list = element("div", "reference-list")
    for (const view of views) list.append(this.createReferenceItem(view))
    container.append(list)
    return container
  }

  // 多引卡内的引用项:结构同单条卡,但不重复 claim_text(共享块在卡顶)。
  createReferenceItem(view) {
    const state = displayState(view.state)
    const nestedClass = ["nested", "carry_forward"].includes(view.raw.reference_role) ? " is-nested" : ""
    const item = element("section", `reference-item is-${state}${nestedClass}`)
    const body = this.createItemBody(view)
    item.append(this.createItemHeader(view, state === "pass", body))
    body.hidden = state === "pass"
    item.append(body)
    return item
  }

  // 卡头:条名(15/600) + 角色前缀 + 状态 pill(+展开箭头)。
  // 未通过/待核实是静态展开行;已通过的卡头可点击展开正文。
  createItemHeader(view, interactive, body) {
    const state = displayState(view.state)
    const header = element(interactive ? "button" : "div", "reference-item-header")
    if (interactive) {
      header.type = "button"
      header.setAttribute("aria-expanded", "false")
      header.addEventListener("click", () => {
        const expanded = body.hidden === false
        body.hidden = expanded
        header.setAttribute("aria-expanded", String(!expanded))
      })
    }
    const label = element("span", "reference-label")
    const role = REFERENCE_ROLE_LABELS[view.raw.reference_role]
    if (role) label.append(element("span", "reference-role-prefix", `${role} · `))
    if (view.candidateCitation) label.append(element("span", "reference-context-label", "原引用"))
    label.append(element("span", "reference-title", view.reference))
    header.append(label)
    header.append(element("span", `status-pill is-${state}`, STATUS_PILL_TEXT[view.state] || "未核查"))
    if (interactive) header.append(element("span", "reference-chevron", "▸"))
    return header
  }

  // 正文:建议 → 候选修正引用 → 参考案例 → 权威来源 → 决策按钮。
  createItemBody(view, options = {}) {
    const body = element("div", "reference-item-body")
    if (options.includeQuote) {
      body.append(this.createQuoteZone(
        view.raw.claim_text || "",
        view.raw,
        view.raw.card_id || view.checkId,
        view.raw.note_context || null,
      ))
    }
    if (view.verdict?.suggestion) {
      const block = element("div", "suggestion-block")
      block.append(element("p", "card-suggestion", view.verdict.suggestion))
      body.append(block)
    }
    const candidateCard = view.candidateCitation ? this.createCandidateCitation(view.candidateCitation) : null
    if (candidateCard) body.append(candidateCard)
    if (view.candidates?.length) body.append(this.createCaseCandidates(view.candidates))
    if (view.verificationMode === "existence") body.append(this.createExistenceResult(view.authoritySources))
    else if (view.evidence) body.append(this.createAuthorityBlock(view.evidence))
    body.append(this.createDecisionRow(view, candidateCard))
    return body
  }

  // 文书原文块:浅灰底,"文书原文/定位原文"整合进块内,不再依赖外部分区标签。
  createQuoteZone(quoteText, jumpTarget, locateId, noteContext = null) {
    const zone = element("div", "doc-quote")
    const head = element("div", "doc-quote-header")
    head.append(element("span", "doc-quote-label", noteContext ? "脚注原文" : "文书原文"))
    const displayLocations = jumpTarget?.display_locations
      || (jumpTarget?.source_locations || []).map((location, index) => ({ ...location, source_location_index: index }))
    if (displayLocations.length) {
      const locationCount = displayLocations.length
      const buttons = locationCount > 1
        ? displayLocations.map((location, index) => ({ text: `定位 ${index + 1}`, index: location.source_location_index }))
        : [{ text: "定位原文", index: displayLocations[0].source_location_index }]
      const actions = element("span", "doc-quote-actions")
      for (const { text, index } of buttons) {
        const jump = element("button", "action-button jump-button", text)
        jump.type = "button"
        jump.dataset.locateId = locateId
        jump.addEventListener("click", () => this.handlers.onJump?.(jumpTarget, index))
        actions.append(jump)
      }
      head.append(actions)
    }
    zone.append(head, element("div", "doc-quote-text", quoteText || ""))
    return zone
  }

  // 权威来源块:浅蓝底 + 3px 品牌蓝竖条,直接可见(不再 details 折叠);
  // 链接固定为"查看权威原文 ↗",URL 不直接外露。
  createAuthorityBlock(evidence) {
    const hasText = evidence.articleHeading || evidence.structurePath
      || evidence.articleText || evidence.related.length
    if (!hasText && evidence.url) {
      const row = element("div", "authority-link-row")
      const link = element("a", "authority-link", "查看权威来源 ↗")
      link.href = evidence.url
      link.target = "_blank"
      link.rel = "noopener noreferrer"
      row.append(link)
      return row
    }
    const block = element("div", `authority-block${evidence.comparison ? " is-comparison" : ""}`)
    if (evidence.articleHeading) block.append(element("div", "authority-heading", evidence.articleHeading))
    if (evidence.structurePath) block.append(element("div", "authority-meta", `章节位置:${evidence.structurePath}`))
    if (evidence.articleText) block.append(element("div", "authority-text", evidence.articleText))
    for (const item of evidence.related) {
      const line = element("div", item.tone ? `authority-reference is-${item.tone}` : "authority-text")
      if (item.heading) line.append(element("strong", "", item.heading), "　")
      line.append(item.text)
      block.append(line)
    }
    if (evidence.url) {
      const row = element("div", "authority-link-row")
      const link = element("a", "authority-link", "查看权威原文 ↗")
      link.href = evidence.url
      link.target = "_blank"
      link.rel = "noopener noreferrer"
      row.append(link)
      block.append(row)
    }
    return block
  }

  createExistenceResult(sources = []) {
    const result = element("div", "existence-result")
    result.append(element("span", "existence-status", "✓ 存在性核验通过"))
    if (sources.length === 1) {
      const link = element("a", "existence-link", `查看《${sources[0].title}》权威来源 →`)
      link.href = sources[0].url
      link.target = "_blank"
      link.rel = "noopener noreferrer"
      result.append(link)
    } else if (sources.length > 1) {
      const details = element("details", "existence-sources")
      details.append(element("summary", "existence-link", `查看 ${sources.length} 个权威来源`))
      for (const source of sources) {
        const link = element("a", "existence-source-link", `《${source.title}》 →`)
        link.href = source.url
        link.target = "_blank"
        link.rel = "noopener noreferrer"
        details.append(link)
      }
      result.append(details)
    }
    return result
  }

  createCandidateCitation(candidate) {
    const card = element("div", "candidate-citation")
    card.append(
      element("div", "candidate-citation-label", "候选修正引用"),
      element("div", "candidate-citation-text", candidate.text),
    )
    return card
  }

  createCaseCandidates(candidates) {
    const box = element("div", "case-candidates")
    box.append(element("div", "authority-meta", `参考案例(${candidates.length})`))
    candidates.forEach((candidate, index) => {
      const line = element("div", "candidate-line")
      line.append(`${index + 1}. ${candidate.title || "未命名案例"}`)
      const metadata = [candidate.case_number, candidate.court, candidate.last_instance_date].filter(Boolean).join("｜")
      if (metadata) line.append(element("div", "candidate-meta", metadata))
      if (candidate.url) {
        line.append(" ")
        const link = element("a", "authority-link", "查看原文 ↗")
        link.href = candidate.url
        link.target = "_blank"
        link.rel = "noopener noreferrer"
        line.append(link)
      }
      box.append(line)
    })
    return box
  }

  // 决策按钮:存在 machine_applicable 修订时显示,右下对齐,接受 ⇄ 取消。
  createDecisionRow(view, candidateCard = null) {
    const row = element("div", "decision-row")
    if (!hasMachineFix(view.raw)) return row
    const button = element("button", "decision-button")
    button.type = "button"
    button.dataset.decision = "accepted"
    button.dataset.checkId = view.checkId
    const sync = () => {
      const accepted = this.decisions[view.checkId] === "accepted"
      button.textContent = accepted ? "取消修订" : "接受修订"
      button.classList.toggle("is-active", accepted)
      candidateCard?.classList.toggle("is-accepted", accepted)
      button.disabled = false
    }
    // 供 setDecision 原地刷新本按钮,避免整表重渲染导致列表跳动
    this.decisionSyncers.set(view.checkId, sync)
    sync()
    button.addEventListener("click", async () => {
      button.disabled = true
      try {
        if (this.decisions[view.checkId] === "accepted") {
          await this.handlers.onUndoFix?.(view.raw)
        } else {
          await this.handlers.onApplyFix?.(view.raw)
        }
      } finally {
        sync()
      }
    })
    row.append(button)
    return row
  }

  // 原地更新某条结果的决策状态与按钮文案(不重建列表、不改变滚动位置)
  setDecision(checkId, decision) {
    if (decision) {
      this.decisions[checkId] = decision
    } else {
      delete this.decisions[checkId]
    }
    this.decisionSyncers.get(checkId)?.()
  }

  showMessage(message) {
    const node = document.getElementById("message")
    node.textContent = message
    node.classList.remove("is-hidden")
    clearTimeout(this.messageTimer)
    this.messageTimer = setTimeout(() => node.classList.add("is-hidden"), 5000)
  }
}

function element(tag, className = "", text = "") {
  const node = document.createElement(tag)
  if (className) node.className = className
  if (text) node.textContent = text
  return node
}

function formatCheckedAt(iso) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ""
  const now = new Date()
  const time = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`
  if (date.toDateString() === now.toDateString()) return `今天 ${time}`
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (date.toDateString() === yesterday.toDateString()) return `昨天 ${time}`
  return `${date.getMonth() + 1}月${date.getDate()}日 ${time}`
}
