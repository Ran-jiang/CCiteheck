export async function connectToWord() {
  if (!window.Office) {
    throw new Error("未加载 Office.js")
  }
  const info = await Office.onReady()
  if (info.host !== Office.HostType.Word) {
    throw new Error("请在 Microsoft Word 中打开此插件")
  }
}

export async function getDocumentName() {
  const directUrl = Office.context.document.url || ""
  const propertiesUrl = directUrl || await getDocumentUrlFromProperties()
  if (!propertiesUrl) return "未命名文档.docx"
  const path = propertiesUrl.split(/[?#]/, 1)[0]
  const fileName = decodeURIComponent(path.split(/[\\/]/).pop() || "")
  return fileName || "未命名文档.docx"
}

function getDocumentUrlFromProperties() {
  if (!Office.context.document.getFilePropertiesAsync) return Promise.resolve("")
  return new Promise(resolve => {
    Office.context.document.getFilePropertiesAsync(result => {
      resolve(result.status === Office.AsyncResultStatus.Succeeded ? result.value?.url || "" : "")
    })
  })
}

export async function getDocumentBase64() {
  const file = await getCompressedFile()
  try {
    const slices = []
    let totalLength = 0
    for (let index = 0; index < file.sliceCount; index += 1) {
      const slice = await getSlice(file, index)
      const bytes = Uint8Array.from(slice.data)
      slices.push(bytes)
      totalLength += bytes.length
    }
    const documentBytes = new Uint8Array(totalLength)
    let offset = 0
    for (const slice of slices) {
      documentBytes.set(slice, offset)
      offset += slice.length
    }
    return bytesToBase64(documentBytes)
  } finally {
    file.closeAsync()
  }
}

export async function getSelectedContent() {
  if (!window.Word?.run) throw new Error("当前 Word 版本不支持选区坐标")
  return Word.run(async context => {
    const selection = context.document.getSelection()
    const bodyParagraphs = context.document.body.paragraphs
    const selectedParagraphs = selection.paragraphs
    const tables = context.document.body.tables
    const includeNotes = Office.context.requirements.isSetSupported("WordApi", "1.5")
    const noteCollections = includeNotes
      ? [["footnote", context.document.body.footnotes], ["endnote", context.document.body.endnotes]]
      : []
    selection.load("start,end")
    bodyParagraphs.load("items/tableNestingLevel")
    selectedParagraphs.load("items")
    tables.load("items/rowCount,items/columnCount")
    for (const [, collection] of noteCollections) collection.load("items")
    await context.sync()

    const bodyRanges = bodyParagraphs.items.map(paragraph => paragraph.getRange())
    const selectedRanges = selectedParagraphs.items.map(paragraph => paragraph.getRange())
    for (const range of [...bodyRanges, ...selectedRanges]) range.load("start,end,text")

    const cells = []
    for (let tableIndex = 0; tableIndex < tables.items.length; tableIndex += 1) {
      const table = tables.items[tableIndex]
      for (let rowIndex = 0; rowIndex < table.rowCount; rowIndex += 1) {
        for (let cellIndex = 0; cellIndex < table.columnCount; cellIndex += 1) {
          const cell = table.getCell(rowIndex, cellIndex)
          cell.body.paragraphs.load("items")
          cells.push({ tableIndex, rowIndex, cellIndex, cell })
        }
      }
    }
    const notes = []
    for (const [noteType, collection] of noteCollections) {
      collection.items.forEach((note, noteIndex) => {
        note.body.paragraphs.load("items")
        notes.push({ noteType, noteIndex, note })
      })
    }
    await context.sync()

    for (const item of cells) {
      item.ranges = item.cell.body.paragraphs.items.map(paragraph => paragraph.getRange())
      for (const range of item.ranges) range.load("start,end,text")
    }
    for (const item of notes) {
      item.ranges = item.note.body.paragraphs.items.map(paragraph => paragraph.getRange())
      for (const range of item.ranges) range.load("start,end,text")
    }
    await context.sync()

    const locations = new Map()
    let topLevelIndex = 0
    for (let index = 0; index < bodyParagraphs.items.length; index += 1) {
      if (bodyParagraphs.items[index].tableNestingLevel !== 0) continue
      locations.set(bodyRanges[index].start, {
        block_id: `word:p:${topLevelIndex}`,
        char_start: 0,
      })
      topLevelIndex += 1
    }
    for (const item of cells) {
      let charStart = 0
      for (const range of item.ranges) {
        const text = normalizeWordText(range.text)
        if (!text) continue
        if (!locations.has(range.start)) {
          locations.set(range.start, {
            block_id: `word:t:${item.tableIndex}:${item.rowIndex}:${item.cellIndex}`,
            char_start: charStart,
          })
        }
        charStart += text.length + 1
      }
    }
    for (const item of notes) {
      item.ranges.forEach((range, paragraphIndex) => {
        if (!normalizeWordText(range.text)) return
        locations.set(range.start, {
          block_id: `word:${item.noteType}:${item.noteIndex}:${paragraphIndex}`,
          char_start: 0,
        })
      })
    }
    const lines = []
    const sourceBlocks = []
    for (const range of selectedRanges) {
      const source = locations.get(range.start)
      if (!source) continue
      const rawStart = Math.max(selection.start, range.start) - range.start
      const rawEnd = Math.min(selection.end, range.end) - range.start
      const line = normalizeWordText(range.text.slice(rawStart, rawEnd))
      if (!line) continue
      lines.push(line)
      sourceBlocks.push({
        block_id: source.block_id,
        char_start: source.char_start + normalizeWordText(range.text.slice(0, rawStart)).length,
      })
    }
    return { text: lines.join("\n"), source_blocks: sourceBlocks }
  })
}

function normalizeWordText(text) {
  return String(text || "").replace(/[\t\n\r]/g, " ").replace(/ +/g, " ").trim()
}

function getCompressedFile() {
  return new Promise((resolve, reject) => {
    Office.context.document.getFileAsync(
      Office.FileType.Compressed,
      { sliceSize: 1024 * 1024 },
      result => result.status === Office.AsyncResultStatus.Succeeded
        ? resolve(result.value)
        : reject(new Error(result.error.message))
    )
  })
}

function getSlice(file, index) {
  return new Promise((resolve, reject) => {
    file.getSliceAsync(index, result => result.status === Office.AsyncResultStatus.Succeeded
      ? resolve(result.value)
      : reject(new Error(result.error.message)))
  })
}

function bytesToBase64(bytes) {
  const chunkSize = 32768
  let binary = ""
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
  }
  return btoa(binary)
}
