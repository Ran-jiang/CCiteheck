import assert from "node:assert/strict"
import test from "node:test"

import {
  getDocumentName,
  getSelectedContent,
} from "../assets/office-document.js"
import {
  escapeSearchText,
  jumpToSource,
  planSearchPieces,
  seedSourceBookmarks,
  validateSearchSpan,
} from "../assets/word-bookmarks.js"


test("getDocumentName reads the real filename from the Word URL", async () => {
  globalThis.Office = {
    context: { document: { url: "https://example.com/contracts/%E5%90%88%E5%90%8C%E5%AE%A1%E6%9F%A5.docx?web=1" } },
  }
  assert.equal(await getDocumentName(), "合同审查.docx")
})


test("getDocumentName falls back to Word file properties", async () => {
  globalThis.Office = {
    AsyncResultStatus: { Succeeded: "succeeded" },
    context: { document: {
      url: "",
      getFilePropertiesAsync(callback) {
        callback({ status: "succeeded", value: { url: "C:\\Legal\\法律意见书.docx" } })
      },
    } },
  }
  assert.equal(await getDocumentName(), "法律意见书.docx")
})

test("getSelectedContent keeps the original block and in-block offset", async () => {
  globalThis.Office = { context: { requirements: { isSetSupported() { return false } } } }
  const firstRange = { start: 0, end: 3, text: "标题\r", load() {} }
  const sourceRange = { start: 100, end: 107, text: "前文依据法条。", load() {} }
  const first = { tableNestingLevel: 0, getRange() { return firstRange } }
  const source = { tableNestingLevel: 0, getRange() { return sourceRange } }
  const selection = {
    start: 102,
    end: 106,
    paragraphs: { items: [source], load() {} },
    load() {},
  }
  const Word = { async run(callback) { return callback({
    document: {
      getSelection() { return selection },
      body: {
        paragraphs: { items: [first, source], load() {} },
        tables: { items: [], load() {} },
        footnotes: { items: [], load() {} },
        endnotes: { items: [], load() {} },
      },
    },
    async sync() {},
  }) } }
  globalThis.Word = Word
  globalThis.window = { Word }
  assert.deepEqual(await getSelectedContent(), {
    text: "依据法条",
    source_blocks: [{ block_id: "word:p:1", char_start: 2 }],
  })
})


test("seedSourceBookmarks replaces old markers and jumpToSource selects the bookmark", async () => {
  globalThis.Office = { context: { requirements: { isSetSupported() { return false } } } }
  const calls = []
  const sentence = {
    insertBookmark(name) { calls.push(["insert", name]) },
  }
  const paragraphRange = {
    search(text, options) {
      assert.equal(text, "裁判理由完整一句。")
      assert.equal(options.matchWildcards, false)
      return { items: [sentence], load() {} }
    },
  }
  const bodyRange = { getBookmarks() { return { value: ["_CCOLD_0", "_Toc42"] } }, search() { return { items: [], load() {} } } }
  const body = {
    paragraphs: {
      items: [{ tableNestingLevel: 0, getRange() { return paragraphRange } }],
      load() {},
    },
    tables: { items: [], load() {} },
    footnotes: { items: [], load() {} },
    endnotes: { items: [], load() {} },
    getRange() { return bodyRange },
  }
  const bookmark = {
    isNullObject: false,
    load() {},
    select() { calls.push(["select", "bookmark"]) },
  }
  const Word = { async run(callback) { return callback({
    document: {
      body,
      deleteBookmark(name) { calls.push(["delete", name]) },
      getBookmarkRangeOrNullObject(name) {
        assert.equal(name, calls.find(call => call[0] === "insert")[1])
        return bookmark
      },
    },
    async sync() {},
  }) } }
  globalThis.Word = Word
  globalThis.window = { Word }
  const location = {
    platform: "docx", block_id: "word:p:0", char_start: 0, char_end: 9,
    anchor_text: "裁判理由完整一句。",
  }
  const details = await seedSourceBookmarks({
    document_key: "sha256:test",
    verification: {
      statute_results: [{ card_id: "card_1", source_locations: [location] }],
      case_results: [],
    },
  })
  assert.deepEqual(details.failed, [])
  const insertedName = calls.find(call => call[0] === "insert")[1]
  assert.match(insertedName, /^_cc[a-z0-9]+_[a-z0-9_]+_0$/)
  assert.deepEqual(calls, [["delete", "_CCOLD_0"], ["insert", insertedName]])
  const jump = await jumpToSource(
    { card_id: "card_1", source_locations: [location] },
    "sha256:test",
  )
  assert.equal(jump.method, "bookmark")
  assert.deepEqual(calls.at(-1), ["select", "bookmark"])
})

test("search planner keeps short anchors whole, splits long and multiline anchors, and escapes carets", () => {
  assert.deepEqual(planSearchPieces("第一段。\n第二段。"), ["第一段。", "第二段。"])
  const long = `${"甲".repeat(160)}${"乙".repeat(160)}`
  assert.deepEqual(planSearchPieces(long), ["甲".repeat(100), "乙".repeat(100)])
  assert.equal(escapeSearchText("依据^p标记"), "依据^^p标记")
  assert.equal(validateSearchSpan("第一段。\r第二段。", ["第一段。", "第二段。"], "第一段。\n第二段。"), true)
  assert.equal(validateSearchSpan(`第一段。${"无关文字".repeat(20)}第二段。`, ["第一段。", "第二段。"], "第一段。\n第二段。"), false)
})

test("seedSourceBookmarks searches inside the target table cell", async () => {
  globalThis.Office = { context: { requirements: { isSetSupported() { return false } } } }
  const calls = []
  const matchedRange = { insertBookmark(name) { calls.push(["insert", name]) } }
  const cellRange = {
    search(text, options) {
      assert.equal(text, "表格中的完整引用句。")
      assert.equal(options.matchWildcards, false)
      calls.push(["cell-search", text])
      return { items: [matchedRange], load() {} }
    },
  }
  const table = {
    rowCount: 3,
    columnCount: 2,
    getCell(row, column) {
      assert.deepEqual([row, column], [2, 1])
      return { body: { getRange() { return cellRange } } }
    },
  }
  const bodyRange = {
    getBookmarks() { return { value: [] } },
    search() { throw new Error("table anchors must not use document body search") },
  }
  const body = {
    paragraphs: { items: [], load() {} },
    tables: { items: [table], load() {} },
    footnotes: { items: [], load() {} },
    endnotes: { items: [], load() {} },
    getRange() { return bodyRange },
  }
  const Word = { async run(callback) { return callback({
    document: { body, deleteBookmark() {} },
    async sync() {},
  }) } }
  globalThis.Word = Word
  globalThis.window = { Word }
  const details = await seedSourceBookmarks({
    document_key: "sha256:table",
    verification: {
      statute_results: [{
        card_id: "card_table",
        source_locations: [{
          platform: "docx",
          block_id: "word:t:0:2:1",
          table_index: 0,
          row_index: 2,
          cell_index: 1,
          anchor_text: "表格中的完整引用句。",
        }],
      }],
      case_results: [],
    },
  })
  assert.deepEqual(details.failed, [])
  assert.deepEqual(calls.map(call => call[0]), ["cell-search", "insert"])
})

test("jumpToSource repairs a missing bookmark with search and occurrence", async () => {
  globalThis.Office = { context: { requirements: { isSetSupported() { return false } } } }
  const calls = []
  const first = { insertBookmark() { calls.push("wrong-insert") }, select() { calls.push("wrong-select") } }
  const second = {
    insertBookmark() { calls.push("insert") },
    select() { calls.push("select") },
  }
  const paragraphRange = {
    search() { return { items: [first, second], load() {} } },
  }
  const body = {
    paragraphs: {
      items: [{ tableNestingLevel: 0, getRange() { return paragraphRange } }],
      load() {},
    },
    footnotes: { items: [], load() {} },
    endnotes: { items: [], load() {} },
    tables: { items: [], load() {} },
    getRange() { return {
      getBookmarks() { return { value: [] } },
      search() { return { items: [], load() {} } },
    } },
  }
  const Word = { async run(callback) { return callback({
    document: {
      body,
      getBookmarkRangeOrNullObject() { return { isNullObject: true, load() {} } },
    },
    async sync() {},
  }) } }
  globalThis.Word = Word
  globalThis.window = { Word }
  await seedSourceBookmarks({
    document_key: "sha256:repair",
    verification: { statute_results: [], case_results: [] },
  })
  const result = await jumpToSource({
    card_id: "card_repair",
    source_locations: [{
      platform: "docx",
      block_id: "word:p:0",
      anchor_text: "重复出现的法条引用。",
      occurrence: 1,
    }],
  }, "sha256:repair")
  assert.equal(result.method, "text_repair")
  assert.equal(result.search_method, "block_search")
  assert.deepEqual(calls, ["insert", "select"])
})

test("jumpToSource repairs a missing table bookmark with an exact cell range", async () => {
  globalThis.Office = { context: { requirements: { isSetSupported() { return false } } } }
  const calls = []
  const match = {
    insertBookmark() { calls.push("insert") },
    select() { calls.push("select") },
  }
  const cellRange = {
    search(text) {
      assert.equal(text, "只定位这一句。")
      calls.push("cell-search")
      return { items: [match], load() {} }
    },
  }
  const table = {
    rowCount: 1,
    columnCount: 1,
    getCell(row, column) {
      assert.deepEqual([row, column], [0, 0])
      return { body: { getRange() { return cellRange } } }
    },
  }
  const body = {
    paragraphs: { items: [], load() {} },
    tables: { items: [table], load() {} },
    footnotes: { items: [], load() {} },
    endnotes: { items: [], load() {} },
    getRange() { return {
      getBookmarks() { return { value: [] } },
      search() { throw new Error("must not search the whole document") },
    } },
  }
  const Word = { async run(callback) { return callback({
    document: {
      body,
      getBookmarkRangeOrNullObject() { return { isNullObject: true, load() {} } },
    },
    async sync() {},
  }) } }
  globalThis.Word = Word
  globalThis.window = { Word }
  const result = await jumpToSource({
    card_id: "card_table_repair",
    source_locations: [{
      platform: "docx",
      block_id: "word:t:0:0:0",
      table_index: 0,
      row_index: 0,
      cell_index: 0,
      anchor_text: "只定位这一句。",
    }],
  }, "sha256:table-repair")
  assert.equal(result.method, "text_repair")
  assert.equal(result.search_method, "cell_search")
  assert.deepEqual(calls, ["cell-search", "insert", "select"])
})

test("jumpToSource requires structured Word coordinates", async () => {
  globalThis.Office = { context: { requirements: { isSetSupported() { return false } } } }
  globalThis.Word = { async run() {} }
  globalThis.window = { Word: globalThis.Word }
  await assert.rejects(
    () => jumpToSource({ card_id: "card_1", source_locations: [] }, "sha256:test"),
    /缺少 Word 块定位信息/,
  )
})
