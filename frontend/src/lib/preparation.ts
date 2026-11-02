export type TaskStatus = 'received' | 'parsing' | 'identifying' | 'needs_input' | 'preparing' | 'ready' | 'failed'
export type EventKind = 'started' | 'completed' | 'info' | 'waiting' | 'failed'

export interface TaskEvent {
  sequence: number
  occurred_at: string
  data_version: number
  stage: string
  kind: EventKind
  title: string
  detail: string
}

export interface TaskOptions {
  reconciliation_period: string | null
  mappings: unknown[]
  specification_aliases: Record<string, string>
  date_tolerance_days: number
}

export interface TaskIssue {
  code: string
  message: string
  blocking: boolean
  source_ref?: string | null
}

export interface TaskView {
  task_id: string
  status: TaskStatus
  stage: string
  created_at: string
  updated_at: string
  data_version: number
  sources: { role: string; original_name: string }[]
  options: TaskOptions
  issues: TaskIssue[]
  artifact_names: string[]
  events: TaskEvent[]
}

export interface ConfirmationInput {
  actor: string
  reason: string
  options: TaskOptions
  formula_values: Record<string, string>
  expected_data_version: number
}

export const STATUS_LABELS: Record<TaskStatus, string> = {
  received: '等待执行', parsing: '解析工作簿', identifying: '识别字段',
  needs_input: '等待补充', preparing: '准备数据', ready: '准备完成', failed: '执行失败',
}

export const STAGE_LABELS: Record<string, string> = {
  received: '接收文件', parse: '解析工作簿', parse_workbooks: '解析工作簿',
  identify: '识别字段', identify_fields: '识别字段', apply_confirmation: '应用人工补充',
  prepare: '准备数据', normalize_records: '标准化明细',
  normalize_and_build_baseline: '标准化明细', build_baseline: '生成 C 初始底稿',
  build_b_index: '建立 B 查询索引', publish_prepared: '保存产物', preparation_ready: '数据准备完成',
}

export const isTerminal = (task: TaskView) => ['ready', 'needs_input', 'failed'].includes(task.status)
export const taskUrl = (id: string) => `/api/preparation/tasks/${encodeURIComponent(id)}`

export async function requestTask(url: string, init?: RequestInit): Promise<TaskView> {
  let response: Response
  try {
    response = await fetch(url, init)
  } catch {
    throw new Error('无法连接后端，执行记录暂时无法更新。请检查服务后重试。')
  }
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.error?.message
      ?? (Array.isArray(payload?.detail)
        ? payload.detail.map((item: { msg: string }) => item.msg).join('；')
        : payload?.detail)
    throw new Error(typeof detail === 'string' ? detail : `请求失败（HTTP ${response.status}），请检查后端服务。`)
  }
  if (!payload || typeof payload.task_id !== 'string' || typeof payload.status !== 'string') {
    throw new Error('后端返回了无法识别的任务状态。')
  }
  return { ...payload, events: payload.events ?? [] } as TaskView
}
