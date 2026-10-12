import assert from "node:assert/strict"
import test from "node:test"
import { applyTrackedRevision, revisionFor, undoTrackedRevision } from "../assets/word-revisions.js"

test("revisionFor selects a machine-applicable issue", () => {
  assert.deepEqual(revisionFor({
    findings: [{ revision: {
      strategy: "replace_exact_text",
      original_text: "依照第十条处理",
      revised_text: "依照第十一条处理",
      machine_applicable: true,
    } }],
  }), { original: "依照第十条处理", revised: "依照第十一条处理" })
})

test("revisionFor rejects missing replacement", () => {
  assert.equal(revisionFor({ claim_text: "原文", findings: [] }), null)
})

test("applyTrackedRevision replaces one exact occurrence with change tracking", async () => {
  const calls = []
  const innerMatch = {
    insertText(text, mode) { calls.push(["replace", text, mode]) },
  }
  const match = {
    search(text, options) {
      calls.push(["inner-search", text, options])
      return { items: [innerMatch], load(field) { assert.equal(field, "items") } }
    },
  }
  const document = {
    changeTrackingMode: "Off",
    load(field) { assert.equal(field, "changeTrackingMode") },
    body: {
      search(text, options) {
        calls.push(["search", text, options])
        return { items: [match], load(field) { assert.equal(field, "items") } }
      },
    },
  }
  globalThis.Office = { context: { requirements: { isSetSupported: () => true } } }
  globalThis.window = { Word: { run: async callback => callback({ document, async sync() {} }) } }
  globalThis.Word = globalThis.window.Word
  const check = { findings: [{ revision: {
    strategy: "replace_exact_text",
    original_text: "依据《民法典》第五百零九条第九款处理。",
    revised_text: "依据《民法典》第五百零九条第一款处理。",
    machine_applicable: true,
  } }] }
  const result = await applyTrackedRevision(check)
  assert.deepEqual(result, {
    method: "unique_text",
    revised_text: "依据《民法典》第五百零九条第一款处理。",
  })
  assert.equal(calls.find(call => call[0] === "inner-search")[1], "九")
  assert.deepEqual(calls.at(-1), ["replace", "一", "Replace"])
  assert.equal(document.changeTrackingMode, "Off")
})

test("applyTrackedRevision changes only the law-name difference", async () => {
  const calls = []
  const innerMatch = { insertText(text, mode) { calls.push(["replace", text, mode]) } }
  const match = { search(text) {
    calls.push(["inner-search", text])
    return { items: [innerMatch], load() {} }
  } }
  const document = {
    changeTrackingMode: "Off", load() {},
    body: { search(text) {
      calls.push(["search", text])
      return { items: [match], load() {} }
    } },
  }
  globalThis.Office = { context: { requirements: { isSetSupported: () => true } } }
  globalThis.window = { Word: { run: async callback => callback({ document, async sync() {} }) } }
  globalThis.Word = globalThis.window.Word
  await applyTrackedRevision({ findings: [{ revision: {
    strategy: "replace_exact_text",
    original_text: "根据《中华人民共和国著作产权实施条例》，复制、改编等权利各有范围。",
    revised_text: "根据《中华人民共和国著作权法实施条例》，复制、改编等权利各有范围。",
    machine_applicable: true,
  } }] })
  assert.deepEqual(calls.find(call => call[0] === "inner-search"), ["inner-search", "产权"])
  assert.deepEqual(calls.at(-1), ["replace", "权法", "Replace"])
})

test("applyTrackedRevision refuses ambiguous document text", async () => {
  const document = {
    changeTrackingMode: "Off", load() {},
    body: { search() { return { items: [{}, {}], load() {} } } },
  }
  globalThis.Office = { context: { requirements: { isSetSupported: () => true } } }
  globalThis.window = { Word: { run: async callback => callback({ document, async sync() {} }) } }
  globalThis.Word = globalThis.window.Word
  await assert.rejects(() => applyTrackedRevision({ findings: [{ revision: {
    strategy: "replace_exact_text", original_text: "重复原文", revised_text: "修订原文", machine_applicable: true,
  } }] }), /原文存在多处相同内容/)
})

test("undoTrackedRevision restores the original text", async () => {
  const calls = []
  const innerMatch = { insertText(value) { calls.push(["replace", value]) } }
  const document = {
    changeTrackingMode: "Off", load() {},
    body: { search(text) {
      calls.push(["search", text])
      return { items: [{ search(value) {
        calls.push(["inner-search", value])
        return { items: [innerMatch], load() {} }
      } }], load() {} }
    } },
  }
  globalThis.Office = { context: { requirements: { isSetSupported: () => true } } }
  globalThis.window = { Word: { run: async callback => callback({ document, async sync() {} }) } }
  globalThis.Word = globalThis.window.Word
  const result = await undoTrackedRevision({ findings: [{ revision: {
    strategy: "replace_exact_text", original_text: "原文", revised_text: "修订文", machine_applicable: true,
  } }] })
  assert.deepEqual(calls, [["search", "修订文"], ["inner-search", "修订"], ["replace", "原"]])
  assert.deepEqual(result, { method: "unique_text", restored_text: "原文" })
})
