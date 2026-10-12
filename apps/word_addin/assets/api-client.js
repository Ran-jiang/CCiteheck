import { assertCheckResponse, assertServiceContract } from "/shared-assets/verification-contract.js"

export async function checkDocument(payload) {
  // /api/checks 为保活流式响应：处理期间发送前导空白避免 WKWebView 超时，
  // JSON.parse 会忽略前导空白；若处理中出错，正文为带 __stream_error__ 的对象。
  const response = await fetch("/api/checks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(error?.detail || `核验服务返回 ${response.status}`)
  }
  const result = JSON.parse(await response.text())
  if (result && result.__stream_error__) {
    throw new Error(result.detail || "核查失败")
  }
  return assertCheckResponse(result)
}

export async function checkSelection(payload) {
  const response = await fetch("/api/checks/selection", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
  if (!response.ok) {
    const error = await response.json().catch(() => null)
    throw new Error(error?.detail || `核验服务返回 ${response.status}`)
  }
  return assertCheckResponse(await response.json())
}

export async function checkHealth() {
  const response = await fetch("/api/health")
  if (!response.ok) throw new Error("核验服务不可用")
  return assertServiceContract(await response.json())
}

export async function listModels() {
  const response = await fetch("/api/models")
  if (!response.ok) throw new Error("无法获取可用模型")
  return response.json()
}

export async function captureDebugEvent(payload) {
  await fetch("/api/debug-events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
}
