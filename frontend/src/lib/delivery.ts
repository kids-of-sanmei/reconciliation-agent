import type { TaskEvent } from './preparation'

export interface ArtifactRef { name: string; relative_path: string; sha256: string }
export interface VerificationIssue {
  code: string; stage: string; message: string; output_location: string | null
  a_record_id: string | null; b_record_id: string | null
}
export interface DeliveryState {
  task_id: string; data_version: number; match_plan_version: number; generation_version: number
  status: 'running' | 'completed' | 'completed_with_review' | 'needs_input' | 'failed'
  stage: string; attempt: number
  events: Pick<TaskEvent, 'sequence' | 'occurred_at' | 'stage' | 'detail'>[]
  issues: VerificationIssue[]; result_ref: ArtifactRef | null; diagnostic_refs: ArtifactRef[]
}
export interface DeliveryResult {
  status: DeliveryState['status']; generation_version: number
  artifacts: { kind: string; reference: ArtifactRef }[]
  statistics: {
    a_detail_count: number; b_current_month_original_rows: number; b_current_month_business_records: number
    taken_business_records: number; missing_business_records: number; excluded_business_records: number
    totals: { sheet_name: string; quantity_difference: string; amount_difference: string }[]
  }
}
export interface VerificationReport {
  generation_version: number; passed: boolean
  checks: { name: string; status: 'pass' | 'fail' | 'not_run'; detail: string }[]
}
export const deliveryUrl = (taskId: string) => `/api/delivery/tasks/${encodeURIComponent(taskId)}`
export const deliveryLabels: Record<DeliveryState['status'], string> = {
  running: '正在制表并核查', completed: '交付验收通过', completed_with_review: '交付验收通过 · 业务待复核',
  needs_input: '等待补充或验证能力', failed: '验收未通过',
}
export const deliveryFiles: Record<string, string> = {
  c_workbook: 'C 对账工作簿', annotated_b: 'B 反向核查副本', verification_json: '结构化核查报告',
  verification_readable: '可阅读核查报告', reviews: '待复核业务明细', manifest: '来源与生成版本说明',
}
export const checkLabels: Record<string, string> = {
  a_integrity: 'A 原始业务完整性', b_trace: '实际 B 追溯与精度', allocations: '分配和余额',
  a_c_totals: 'A/C 原始业务合计', aggregate_formulas: '小计和合计公式范围',
  b_original_and_annotation: 'B 原值及实际标注', b_monthly_coverage: 'B 当月覆盖',
  c_formula_recalculation: 'C 公式重算及错误', b_formula_recalculation: 'B 公式重算及错误',
  c_display: 'C 显示与顶部视图', b_display: 'B 显示与顶部视图',
}
