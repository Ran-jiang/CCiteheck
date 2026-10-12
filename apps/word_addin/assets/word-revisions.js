export function revisionFor(check) {
  const proposal = (check.findings || []).map(item => item.revision).find(item =>
    item?.machine_applicable && item.strategy === "replace_exact_text" && item.original_text && item.revised_text
  )
  if (!proposal) return null
  return { original: proposal.original_text, revised: proposal.revised_text }
}

export async function undoTrackedRevision(check) {
  return replaceTrackedText(check, true)
}

export async function applyTrackedRevision(check) {
  return replaceTrackedText(check, false)
}

function changedSegment(from, to) {
  let start = 0
  while (start < from.length && start < to.length && from[start] === to[start]) start += 1
  let fromEnd = from.length
  let toEnd = to.length
  while (fromEnd > start && toEnd > start && from[fromEnd - 1] === to[toEnd - 1]) {
    fromEnd -= 1
    toEnd -= 1
  }
  const original = from.slice(start, fromEnd)
  const revised = to.slice(start, toEnd)
  return original && revised ? { original, revised } : null
}

async function replaceTrackedText(check, undo) {
  if (!window.Word?.run || !Office.context.requirements.isSetSupported("WordApi", "1.4")) {
    throw new Error(undo
      ? "当前 Word 版本不支持自动撤销修订，请在审阅面板中拒绝该修订"
      : "当前 Word 版本不支持自动写入修订，请手动修改")
  }
  const revision = revisionFor(check)
  if (!revision) throw new Error(undo ? "该问题没有可撤销的修订文本" : "该问题没有可安全写入的修订文本")
  const from = undo ? revision.revised : revision.original
  const to = undo ? revision.original : revision.revised
  return Word.run(async context => {
    const document = context.document
    document.load("changeTrackingMode")
    const matches = document.body.search(from, {
      matchCase: true, matchWholeWord: false, matchWildcards: false,
      ignoreSpace: false, ignorePunct: false,
    })
    matches.load("items")
    await context.sync()
    if (matches.items.length !== 1) {
      throw new Error(undo
        ? matches.items.length
          ? "文中存在多处相同的修订后文本，请在审阅面板中拒绝该修订"
          : "未找到已写入的修订文本，可能已被手动改动；请在审阅面板中处理"
        : matches.items.length ? "原文存在多处相同内容，请手动修改" : "原文已变化，请重新核查后再写入修订")
    }
    const previousMode = document.changeTrackingMode
    try {
      document.changeTrackingMode = "TrackAll"
      let target = matches.items[0]
      let replacement = to
      const segment = changedSegment(from, to)
      if (segment && (segment.original !== from || segment.revised !== to)) {
        const innerMatches = target.search(segment.original, {
          matchCase: true, matchWholeWord: false, matchWildcards: false,
          ignoreSpace: false, ignorePunct: false,
        })
        innerMatches.load("items")
        await context.sync()
        if (innerMatches.items.length !== 1) {
          throw new Error(undo ? "无法唯一定位待撤销内容" : "无法唯一定位待修订的条号或法名")
        }
        target = innerMatches.items[0]
        replacement = segment.revised
      }
      target.insertText(replacement, "Replace")
      await context.sync()
    } finally {
      document.changeTrackingMode = previousMode
      await context.sync()
    }
    return undo
      ? { method: "unique_text", restored_text: to }
      : { method: "unique_text", revised_text: to }
  })
}
