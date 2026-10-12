import {
  assertCheckResponse,
  VERIFICATION_SCHEMA_VERSION,
} from "/shared-assets/verification-contract.js"

const STORAGE_KEY = "ccitecheck.recentChecks"
const HISTORY_LIMIT = 3

export function readHistory() {
  const value = localStorage.getItem(STORAGE_KEY)
  if (!value) return []
  try {
    const history = JSON.parse(value)
    return Array.isArray(history)
      ? history.filter(item => item.schemaVersion === VERIFICATION_SCHEMA_VERSION).slice(0, HISTORY_LIMIT)
      : []
  } catch {
    localStorage.removeItem(STORAGE_KEY)
    return []
  }
}

export function recordHistory(result) {
  const entry = {
    fileName: result.file_name,
    documentKey: result.document_key,
    checkedAt: new Date().toISOString(),
    schemaVersion: result.verification.schema_version,
    total: result.summary.total,
    issues: result.summary.issues,
  }
  const history = readHistory().filter(item => item.documentKey !== entry.documentKey)
  const nextHistory = [entry, ...history].slice(0, HISTORY_LIMIT)
  localStorage.setItem(STORAGE_KEY, JSON.stringify(nextHistory))
  saveSnapshot(nextHistory, result)
}

// ---- 完整核查结果快照，跟随最近核查列表保留，供“点击回看”使用 ----

const SNAPSHOTS_KEY = "ccitecheck.resultSnapshots"

function readSnapshotStore() {
  try {
    const store = JSON.parse(localStorage.getItem(SNAPSHOTS_KEY) || "{}")
    return store && typeof store === "object" && !Array.isArray(store) ? store : {}
  } catch {
    return {}
  }
}

function saveSnapshot(history, result) {
  const keys = new Set(history.map(item => item.documentKey))
  const store = readSnapshotStore()
  store[result.document_key] = result
  for (const key of Object.keys(store)) {
    if (!keys.has(key)) delete store[key]
  }
  try {
    localStorage.setItem(SNAPSHOTS_KEY, JSON.stringify(store))
  } catch {
    // localStorage 配额不足时放弃快照，摘要列表仍然可用
    localStorage.removeItem(SNAPSHOTS_KEY)
  }
}

export function readResultSnapshot(documentKey) {
  const snapshot = readSnapshotStore()[documentKey]
  if (!snapshot) return null
  try {
    return assertCheckResponse(snapshot)
  } catch {
    return null
  }
}

// ---- 每条核查项的人工处理标记（接受/忽略），按文档内容哈希持久化 ----

const DECISIONS_KEY = "ccitecheck.decisions"

export function readDecisions(documentKey) {
  try {
    const store = JSON.parse(localStorage.getItem(DECISIONS_KEY) || "{}")
    return store[documentKey] || {}
  } catch {
    return {}
  }
}

export function saveDecision(documentKey, checkId, decision) {
  let store = {}
  try {
    store = JSON.parse(localStorage.getItem(DECISIONS_KEY) || "{}")
  } catch {
    store = {}
  }
  const decisions = store[documentKey] || {}
  if (decision) {
    decisions[checkId] = decision
  } else {
    delete decisions[checkId]
  }
  store[documentKey] = decisions
  localStorage.setItem(DECISIONS_KEY, JSON.stringify(store))
  return decisions
}
