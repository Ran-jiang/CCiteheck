import { captureDebugEvent, checkDocument, checkHealth, checkSelection, listModels } from "./api-client.js"
import { readDecisions, readHistory, readResultSnapshot, recordHistory, saveDecision } from "./history.js"
import {
  connectToWord,
  getDocumentBase64,
  getDocumentName,
  getSelectedContent,
} from "./office-document.js"
import { jumpToSource, seedSourceBookmarks } from "./word-bookmarks.js"
import { CheckUi } from "./ui.js"
import { applyTrackedRevision, undoTrackedRevision } from "./word-revisions.js"

const ui = new CheckUi()
let documentName = "未命名文档.docx"
let lastResult = null
let lastCheckMode = "full"
let lastScope = null

document.getElementById("start-button").addEventListener("click", runCheck)
document.getElementById("selection-button").addEventListener("click", runSelectionCheck)
document.getElementById("rerun-button").addEventListener("click", rerunCurrentCheck)
document.getElementById("brand-button").addEventListener("click", showHome)
document.getElementById("help-button").addEventListener("click", openHelp)

ui.setHandlers({
  onJump: async (check, locationIndex = 0) => {
    try {
      const details = await jumpToSource(check, lastResult.document_key, locationIndex)
      await saveWordDebug("locate_success", check, details)
      if (details.warning) ui.showMessage(details.warning)
    } catch (error) {
      await saveWordDebug("locate_error", check, null, error)
      ui.showMessage(error.message)
    }
  },
  onApplyFix: async check => {
    if (!lastResult) return
    try {
      const details = await applyTrackedRevision(check)
      saveDecision(lastResult.document_key, check.check_id, "accepted")
      await saveWordDebug("revision_applied", check, details)
      // 只更新本卡片状态，不整表重渲染，避免列表跳回顶部
      ui.setDecision(check.check_id, "accepted")
      ui.showMessage("修订已写入 Word，可在审阅面板接受或拒绝")
    } catch (error) {
      await saveWordDebug("revision_error", check, null, error)
      ui.showMessage(error.message)
    }
  },
  onUndoFix: async check => {
    if (!lastResult) return
    try {
      const details = await undoTrackedRevision(check)
      saveDecision(lastResult.document_key, check.check_id, null)
      await saveWordDebug("revision_undone", check, details)
      ui.setDecision(check.check_id, null)
      ui.showMessage("已撤销该修订，文本改回原文")
    } catch (error) {
      await saveWordDebug("revision_undo_error", check, null, error)
      ui.showMessage(error.message)
    }
  },
  onHistoryOpen: openHistorySnapshot,
})

initialize()

async function initialize() {
  ui.renderHistory(readHistory())
  await connect()
}

async function connect() {
  ui.setDocument("正在读取 Word 文档…", "正在连接插件", false)
  try {
    const [, health] = await Promise.all([connectToWord(), checkHealth()])
    documentName = await getDocumentName()
    ui.setDocument(documentName, "已连接 · 可核查全文或选中片段", true)
    document.getElementById("selection-button").disabled = false
    if (!health.pkulaw_configured) ui.showMessage("案例库凭证未配置，司法案例核查当前不可用")
    await loadModels()
  } catch (error) {
    ui.setDocument("无法开始核查", error.message, false)
    document.getElementById("selection-button").disabled = true
  }
}

async function loadModels() {
  const select = document.getElementById("model-select")
  try {
    const { models, default: fallback } = await listModels()
    select.innerHTML = ""
    for (const model of models) {
      const option = document.createElement("option")
      option.value = model.key
      option.textContent = model.configured ? model.label : `${model.label}（未配置密钥）`
      option.disabled = !model.configured
      select.append(option)
    }
    // 必须在全部插入后再设默认值：对未入 DOM 的 option 设 selected 会错乱
    if (fallback) select.value = fallback
    if (!select.value || select.selectedIndex < 0) {
      const first = [...select.options].find(o => !o.disabled)
      if (first) select.value = first.value
    }
  } catch {
    select.innerHTML = "<option value=\"\">默认模型</option>"
  }
}

function checkScope() {
  const select = document.getElementById("model-select")
  const scope = {
    include_statutes: document.getElementById("statute-toggle").checked,
    include_cases: document.getElementById("case-toggle").checked,
    model: select && select.value ? select.value : undefined,
  }
  if (!scope.include_statutes && !scope.include_cases) {
    ui.showMessage("请至少选择一种核查范围（法律法规引用或司法案例引用）")
    return null
  }
  return scope
}

async function runCheck() {
  const scope = checkScope()
  if (!scope) return
  lastScope = scope
  ui.resetProgress()
  ui.setBusy(true)
  ui.showScreen("progress-screen")
  try {
    const docxBase64 = await getDocumentBase64()
    ui.setStage("stage-read", "complete", "DOCX 读取完成")
    ui.setStage("stage-submit", "complete", "已安全提交至核验服务")
    ui.setStage("stage-check", "active", "正在查询法规库并执行语义核验")

    const result = await checkDocument({
      file_name: documentName,
      docx_base64: docxBase64,
      semantic_check: true,
      ...scope,
    })
    await finishCheck(result, "full")
  } catch (error) {
    showHome()
    ui.showMessage(error.message || "核查失败")
  } finally {
    ui.setBusy(false)
  }
}

async function runSelectionCheck() {
  const scope = checkScope()
  if (!scope) return
  lastScope = scope
  try {
    ui.setBusy(true)
    const selection = await getSelectedContent()
    if (!selection.text) {
      ui.showMessage("请先在文档中选中要核查的内容")
      return
    }
    ui.resetProgress()
    ui.setStage("stage-read", "complete", "已读取选中片段")
    ui.showScreen("progress-screen")
    ui.setStage("stage-submit", "complete", "已安全提交至核验服务")
    ui.setStage("stage-check", "active", "正在查询法规库并执行语义核验")

    const result = await checkSelection({
      file_name: documentName,
      text: selection.text,
      source_blocks: selection.source_blocks,
      semantic_check: true,
      ...scope,
    })
    await finishCheck(result, "selection")
  } catch (error) {
    showHome()
    ui.showMessage(error.message || "选中内容核查失败")
  } finally {
    ui.setBusy(false)
  }
}

async function saveWordDebug(event, check, details, error = null) {
  if (!lastResult?.debug_run_id) return
  const officeError = error ? {
    name: error.name,
    message: error.message,
    code: error.code,
    stack: error.stack,
    debugInfo: error.debugInfo,
    innerError: error.innerError,
  } : null
  try {
    await captureDebugEvent({
      run_id: lastResult.debug_run_id,
      event,
      payload: { document_name: documentName, check, details, error: officeError },
    })
  } catch (_) {
    // 调试留档不能影响正常定位操作。
  }
}

async function finishCheck(result, mode) {
  lastResult = result
  lastCheckMode = mode
  if (mode === "full") {
    recordHistory(result)
    ui.renderHistory(readHistory())
  }
  document.getElementById("rerun-button").textContent = mode === "selection" ? "继续核查" : "重新核查"
  ui.setStage("stage-check", "complete", `已识别 ${result.summary.card_total} 个引用句、${result.summary.reference_total} 条引用`)
  let bookmarkError = null
  let anchors = null
  try {
    anchors = await seedSourceBookmarks(result)
    await saveWordDebug("bookmark_seeded", null, anchors)
  } catch (error) {
    bookmarkError = error
    await saveWordDebug("bookmark_seed_error", null, null, error)
  }
  ui.renderResults(result, readDecisions(result.document_key), { scope: lastScope })
  ui.setLocateFailures(anchors?.failed || [])
  if (bookmarkError) ui.showMessage(bookmarkError.message || "原文定位标记创建失败")
  else if (anchors?.failed?.length) ui.showMessage(`${anchors.failed.length} 处原文未能创建定位标记`)
}

function rerunCurrentCheck() {
  return lastCheckMode === "selection" ? runSelectionCheck() : runCheck()
}

function openHistorySnapshot(entry) {
  const snapshot = readResultSnapshot(entry.documentKey)
  if (!snapshot) {
    ui.showMessage("这条记录没有保存结果快照，请重新核查该文档")
    return
  }
  lastResult = snapshot
  lastCheckMode = "full"
  document.getElementById("rerun-button").textContent = "重新核查"
  ui.renderResults(snapshot, readDecisions(snapshot.document_key), { snapshotAt: entry.checkedAt })
}


function showHome() {
  ui.showScreen("home-screen")
}

function openHelp() {
  ui.showScreen("help-screen")
}
