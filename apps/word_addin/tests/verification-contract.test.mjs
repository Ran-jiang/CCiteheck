import assert from "node:assert/strict"
import test from "node:test"

import {
  assertCheckResponse,
  assertRecognitionResponse,
  assertServiceContract,
} from "../../shared/assets/verification-contract.js"


test("accepts the current service and verification schemas", () => {
  const health = {
    claim_schema_version: "0.6",
    verification_schema_version: "0.9",
  }
  const response = { verification: { schema_version: "0.9" } }

  assert.equal(assertServiceContract(health), health)
  assert.equal(assertCheckResponse(response), response)
  assert.equal(assertRecognitionResponse({
    claim_document: { claim_meta: { schema_version: "0.6" } },
  }).claim_document.claim_meta.schema_version, "0.6")
})


test("rejects stale service and verification schemas", () => {
  assert.throws(
    () => assertServiceContract({
      claim_schema_version: "0.3",
      verification_schema_version: "0.6",
    }),
    /版本不一致/,
  )
  assert.throws(
    () => assertCheckResponse({ verification: { schema_version: "0.6" } }),
    /版本不兼容/,
  )
  assert.throws(
    () => assertRecognitionResponse({
      claim_document: { claim_meta: { schema_version: "0.4" } },
    }),
    /版本不兼容/,
  )
})
