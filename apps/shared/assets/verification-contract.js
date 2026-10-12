export const CLAIM_SCHEMA_VERSION = "0.6"
export const VERIFICATION_SCHEMA_VERSION = "0.9"

export function assertServiceContract(health) {
  if (health?.claim_schema_version !== CLAIM_SCHEMA_VERSION
    || health?.verification_schema_version !== VERIFICATION_SCHEMA_VERSION) {
    throw new Error("客户端与核验服务版本不一致，请更新并重启后再试")
  }
  return health
}

export function assertCheckResponse(payload) {
  if (payload?.verification?.schema_version !== VERIFICATION_SCHEMA_VERSION) {
    throw new Error("核验结果版本不兼容，请更新并重启核验服务")
  }
  return payload
}

export function assertRecognitionResponse(payload) {
  if (payload?.claim_document?.claim_meta?.schema_version !== CLAIM_SCHEMA_VERSION) {
    throw new Error("识别结果版本不兼容，请更新并重启调试服务")
  }
  return payload
}
