import { timestamp } from './download'

// xlsx 体积较大，仅在进入演示模式时按需加载
type XlsxModule = typeof import('xlsx')
let xlsxPromise: Promise<XlsxModule> | null = null
function loadXlsx(): Promise<XlsxModule> {
  xlsxPromise ??= import('xlsx')
  return xlsxPromise
}

export interface DemoStats {
  matched: number
  mismatched: number
  onlyA: number
  onlyB: number
}

export interface DemoOutput {
  blob: Blob
  filename: string
  stats: DemoStats
}

type Row = Record<string, unknown>

async function readRows(XLSX: XlsxModule, file: File): Promise<Row[]> {
  const buf = await file.arrayBuffer()
  const wb = XLSX.read(buf, { type: 'array' })
  const sheet = wb.Sheets[wb.SheetNames[0]]
  if (!sheet) return []
  return XLSX.utils.sheet_to_json<Row>(sheet, { defval: '' })
}

/** 取每行第一个字段作为对账关键字段（如订单号 / 流水号） */
function keyOf(row: Row): string {
  const first = Object.values(row)[0]
  return String(first ?? '').trim()
}

/** 判断两行除关键字段外的内容是否一致 */
function sameContent(a: Row, b: Row): boolean {
  const normalize = (r: Row) =>
    Object.keys(r)
      .sort()
      .map((k) => `${k}=${String(r[k] ?? '').trim()}`)
      .join('|')
  return normalize(a) === normalize(b)
}

/**
 * 演示模式：后端不可用时在本地完成比对。
 * 以每行第一列作为唯一键，输出四个分类工作表。
 */
export async function demoReconcile(fileA: File, fileB: File): Promise<DemoOutput> {
  const XLSX = await loadXlsx()
  const [rowsA, rowsB] = await Promise.all([readRows(XLSX, fileA), readRows(XLSX, fileB)])
  if (!rowsA.length || !rowsB.length) {
    throw new Error('账单文件为空或无法解析，请检查文件内容')
  }

  const mapA = new Map<string, Row>()
  const mapB = new Map<string, Row>()
  for (const r of rowsA) {
    const k = keyOf(r)
    if (k) mapA.set(k, r)
  }
  for (const r of rowsB) {
    const k = keyOf(r)
    if (k) mapB.set(k, r)
  }

  const matched: Row[] = []
  const mismatched: Row[] = []
  const onlyA: Row[] = []
  const onlyB: Row[] = []

  for (const [k, ra] of mapA) {
    const rb = mapB.get(k)
    if (!rb) {
      onlyA.push(ra)
    } else if (sameContent(ra, rb)) {
      matched.push(ra)
    } else {
      mismatched.push({ ...ra, __对比结果: '与账单 B 内容不一致' })
    }
  }
  for (const [k, rb] of mapB) {
    if (!mapA.has(k)) onlyB.push(rb)
  }

  const wb = XLSX.utils.book_new()
  const append = (rows: Row[], name: string) => {
    const ws = rows.length ? XLSX.utils.json_to_sheet(rows) : XLSX.utils.aoa_to_sheet([['（无记录）']])
    XLSX.utils.book_append_sheet(wb, ws, name)
  }
  append(matched, '完全一致')
  append(mismatched, '内容不一致')
  append(onlyA, '仅账单A存在')
  append(onlyB, '仅账单B存在')

  const out = XLSX.write(wb, { type: 'array', bookType: 'xlsx' }) as ArrayBuffer
  const blob = new Blob([out], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  })

  return {
    blob,
    filename: `对账结果_${timestamp()}.xlsx`,
    stats: {
      matched: matched.length,
      mismatched: mismatched.length,
      onlyA: onlyA.length,
      onlyB: onlyB.length,
    },
  }
}
