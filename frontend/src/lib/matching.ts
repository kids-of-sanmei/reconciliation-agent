import type { TaskIssue } from './preparation'

export type RowStatus = 'matched' | 'matched_review' | 'partial' | 'unmatched' | 'unresolved' | 'anomaly'
export interface MatchingRules {
  version: string
  date_tolerance_days: number
  specification_aliases: Record<string, string>
  quantity_tolerance: string
  price_tolerance: string
  amount_tolerance: string
  search_budget: number
  max_group_size: number
  max_candidates: number
  max_model_rounds: number
  max_tool_queries: number
}
export interface Candidate {
  candidate_id: string
  a_record_ids: string[]
  b_record_ids: string[]
  eligible: boolean
  relation_type: string
  review_reasons: string[]
  checks: { field: string; passed: boolean; expected: string | null; actual: string | null; delta: string | null; detail: string }[]
  allocations: { a_record_id: string; b_record_id: string; quantity: string; a_amount: string; b_amount: string }[]
}
export interface MatchingState {
  task_id: string
  version: number
  status: 'running' | 'ready' | 'needs_input' | 'failed'
  completed_rows: number
  total_rows: number
  rules: MatchingRules
  issues: TaskIssue[]
  progress: {
    a_record_id: string
    search_complete: boolean
    search_incomplete_reasons: string[]
    candidates: Candidate[]
    proposals: { round: number; error: string | null; proposal: { reason: string } | null }[]
  }[]
}
export interface MatchResults {
  match_plan_version: number
  rows: { a_record_id: string; status: RowStatus; candidate_ids: string[]; reasons: string[]; remaining_quantity: string | null; remaining_amount: string | null }[]
  b_usage: { b_record_id: string; group_ids: string[]; remaining_quantity: string | null; remaining_amount: string | null; excluded: boolean }[]
}
export type MatchDecision =
  | { action: 'accept'; candidate_id: string }
  | { action: 'release'; a_record_ids: string[] }
  | { action: 'exclude_b' | 'restore_b'; b_record_ids: string[] }
  | { action: 'rules'; rules: MatchingRules }

export const ROW_LABELS: Record<RowStatus, string> = {
  matched: '已匹配', matched_review: '已对应 · 待复核', partial: '部分匹配',
  unmatched: '未匹配', unresolved: '待确认', anomaly: '业务异常',
}
export const matchUrl = (id: string) => `/api/matching/tasks/${encodeURIComponent(id)}`

export class MatchRequestError extends Error {
  constructor(message: string, readonly status: number) { super(message) }
}

export async function requestMatch<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = payload?.error?.message ?? (Array.isArray(payload?.detail)
      ? payload.detail.map((item: { msg: string }) => item.msg).join('；') : payload?.detail)
    throw new MatchRequestError(typeof detail === 'string' ? detail : `请求失败（${response.status}）`, response.status)
  }
  if (!payload || typeof payload !== 'object') throw new Error('匹配响应无法识别')
  return payload as T
}
