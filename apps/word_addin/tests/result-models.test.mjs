import assert from "node:assert/strict"
import test from "node:test"

import { buildResultCards, displayLocationsOf } from "../assets/result-models.js"


test("new statute results are grouped with normalized locators", () => {
  const cards = buildResultCards({
    statute_results: [{
      check_id: "vc_1",
      card_id: "card_1",
      display_group_id: "dg_1",
      claim_id: "claim_1",
      claim_text: "引用内容",
      law_title: "民法典",
      cited_locators: [{ article_no: "第二条", paragraph_no: "第三款" }],
      findings: [{ code: "citation_hierarchy_error", risk_level: "HIGH" }],
      outcome: "issue",
      source_locations: [],
    }],
    case_results: [],
  })

  assert.equal(cards[0].references[0].outcome, "issue")
  assert.deepEqual(cards[0].references[0].paragraphs, ["第三款"])
})


test("single case results remain standalone cards", () => {
  const cards = buildResultCards({
    statute_results: [],
    case_results: [{
      check_id: "cc_1",
      display_group_id: "dg_1",
      claim_text: "引用某案",
      lookup_status: "verified",
      findings: [{ code: "case_identity_error", risk_level: "HIGH" }],
      outcome: "issue",
    }],
  })

  assert.equal(cards[0].check_kind, "case")
  assert.equal(cards[0].outcome, "issue")
})


test("statute and case results from the same source range share one card", () => {
  const cards = buildResultCards({
    statute_results: [{
      check_id: "vc_1", card_id: "card_1", display_group_id: "dg_same",
      claim_id: "claim_statute", claim_text: "同一句引用", law_title: "民法典",
      cited_locators: [{ article_no: "第一条" }], findings: [], outcome: "pass",
      source_locations: [],
    }],
    case_results: [{
      check_id: "cc_1", display_group_id: "dg_same", claim_id: "claim_case",
      claim_text: "同一句引用", cited_case_number: "（2021）最高法民申1号",
      lookup_status: "verified", findings: [], outcome: "pass", source_locations: [],
    }],
  })

  assert.equal(cards.length, 1)
  assert.equal(cards[0].references.length, 2)
  assert.deepEqual(cards[0].references.map(item => item.check_kind), ["statute", "case"])
})


test("footnote results retain their body reference location for grouping and sorting", () => {
  const noteContext = {
    note_type: "footnote",
    note_id: "2",
    referenced_from: [{ block_id: "word:p:3", char_start: 12, char_end: 12 }],
  }
  const cards = buildResultCards({
    statute_results: [{
      check_id: "vc_note", card_id: "card_note", display_group_id: "dg_note",
      claim_id: "claim_note", claim_text: "脚注中的法条引用", law_title: "民法典",
      cited_locators: [{ article_no: "第五百零九条" }], findings: [], outcome: "pass",
      source_locations: [{ block_id: "word:footnote:2" }], note_context: noteContext,
    }],
    case_results: [],
  })

  assert.deepEqual(cards[0].note_context, noteContext)
  assert.equal(cards[0].sort_source_locations[0].block_id, "word:p:3")
  assert.equal(cards[0].source_locations[0].block_id, "word:p:3")
})


test("display locations collapse duplicates and overlapping spans without mutating source locations", () => {
  const locations = [
    { block_id: "word:p:1", char_start: 10, char_end: 40, anchor_text: "较完整原文" },
    { block_id: "word:p:1", char_start: 10, char_end: 40, anchor_text: "重复" },
    { block_id: "word:p:1", char_start: 12, char_end: 38, anchor_text: "子串" },
    { block_id: "word:p:1", char_start: 8, char_end: 42, anchor_text: "最完整原文" },
    { block_id: "word:p:1", char_start: 70, char_end: 90, anchor_text: "独立位置" },
    { block_id: "word:p:2", char_start: 8, char_end: 42, anchor_text: "另一段" },
  ]

  const displayed = displayLocationsOf(locations)

  assert.equal(locations.length, 6)
  assert.equal(displayed.length, 3)
  assert.deepEqual(displayed.map(item => item.source_location_index), [3, 4, 5])
  assert.equal(locations[3].source_location_index, undefined)

  const noteReferences = displayLocationsOf([
    { block_id: "word:p:3", char_start: 5, char_end: 5, anchor_text: "同一段正文", occurrence: 0 },
    { block_id: "word:p:3", char_start: 12, char_end: 12, anchor_text: "同一段正文", occurrence: 0 },
  ])
  assert.equal(noteReferences.length, 2, "同段内不同脚注引用点仍是独立位置")

  const splitCitation = displayLocationsOf([
    { block_id: "word:p:56", char_start: 348, char_end: 394, anchor_text: "引用所在首句" },
    { block_id: "word:p:56", char_start: 394, char_end: 420, anchor_text: "连续第二句" },
    { block_id: "word:p:56", char_start: 420, char_end: 465, anchor_text: "连续第三句" },
  ])
  assert.equal(splitCitation.length, 1)
  assert.equal(splitCitation[0].source_location_index, 0, "连续片段应定位到含引用的首个 span")
})
