export interface HeaderSelection {
  start: number | ''
  count: number | ''
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

async function requestError(response: Response): Promise<Error> {
  const payload: unknown = await response.json().catch(() => null)
  if (isRecord(payload)) {
    if (typeof payload.detail === 'string') return new Error(payload.detail)
    if (Array.isArray(payload.detail)) {
      const messages = payload.detail.flatMap((item: unknown) =>
        isRecord(item) && typeof item.msg === 'string' ? [item.msg] : [],
      )
      if (messages.length) return new Error(messages.join('；'))
    }
  }
  return new Error(`请求失败（HTTP ${response.status}），请稍后重试。`)
}

export async function generateHeaderJson(file: File, selection: HeaderSelection): Promise<string> {
  const data = new FormData()
  data.append('file', file)
  for (const [field, value] of [
    ['header_start_row', selection.start], ['header_row_count', selection.count],
  ] as const) {
    if (value === '') continue
    if (!Number.isInteger(value) || value < 1) throw new Error('表头起始行和行数必须为正整数。')
    data.append(field, String(value))
  }

  let uploaded: Response
  try {
    uploaded = await fetch('/api/upload', { method: 'POST', body: data })
  } catch {
    throw new Error('无法连接后端，请检查服务后重试。')
  }
  if (!uploaded.ok) throw await requestError(uploaded)
  const result: unknown = await uploaded.json()
  if (!isRecord(result) || typeof result.file_id !== 'string' || !/^[0-9a-f]{32}$/.test(result.file_id)) {
    throw new Error('后端返回了无法识别的上传结果。')
  }

  let response: Response
  try {
    response = await fetch(`/api/uploads/${result.file_id}/headers.json`)
  } catch {
    throw new Error('文件已上传，读取 JSON 失败，请检查后端连接后重试。')
  }
  if (!response.ok) throw await requestError(response)
  const json = await response.text()
  const structure: unknown = JSON.parse(json)
  if (!isRecord(structure) || !Array.isArray(structure.sheets) || structure.file_id !== result.file_id) {
    throw new Error('后端返回了无法识别的表头 JSON。')
  }
  return json
}
