<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { MatchRequestError, requestMatch } from '../lib/matching'
import { checkLabels, deliveryFiles, deliveryLabels, deliveryUrl, type DeliveryResult, type DeliveryState, type VerificationReport } from '../lib/delivery'

const props = defineProps<{ taskId: string; matchPlanVersion: number }>()
const state = ref<DeliveryState | null>(null)
const result = ref<DeliveryResult | null>(null)
const report = ref<VerificationReport | null>(null)
const busy = ref(false)
const error = ref('')
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
const completed = computed(() => state.value?.status === 'completed' || state.value?.status === 'completed_with_review')
const locked = computed(() => busy.value || state.value?.status === 'running')
const checkStatus = { pass: '通过', fail: '未通过', not_run: '未完成验证' }
const time = (value: string) => new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(value))

function stop() { generation += 1; clearTimeout(timer) }
async function sync(current = generation) {
  try {
    const view = await requestMatch<DeliveryState>(deliveryUrl(props.taskId))
    if (current !== generation) return
    if (view.match_plan_version !== props.matchPlanVersion) { state.value = null; return }
    state.value = view
    if (view.status !== 'running') {
      if (view.result_ref) {
        const data = await requestMatch<DeliveryResult>(`${deliveryUrl(props.taskId)}/result?expected_generation_version=${view.generation_version}`)
        if (current === generation) result.value = data
      }
      if (view.diagnostic_refs.some(item => item.name === 'verification_report.json')) {
        const data = await requestMatch<VerificationReport>(`${deliveryUrl(props.taskId)}/report?expected_generation_version=${view.generation_version}`)
        if (current === generation) report.value = data
      }
    }
    if (current === generation && (busy.value || view.status === 'running')) timer = setTimeout(() => void sync(current), 800)
  } catch (caught) {
    if (current !== generation) return
    if (caught instanceof MatchRequestError && caught.status === 404 && !state.value) return
    if (caught instanceof MatchRequestError && caught.status === 409 && !busy.value && !state.value) {
      error.value = '匹配方案已更新，请为当前版本重新生成交付文件。'
      return
    }
    error.value = caught instanceof Error ? caught.message : '读取制表和核查状态失败'
    if (busy.value || state.value?.status === 'running') timer = setTimeout(() => void sync(current), 2000)
  }
}

async function act(action: 'run' | 'verify', force = false) {
  if (busy.value) return
  stop()
  const current = generation
  busy.value = true
  error.value = ''
  result.value = null
  report.value = null
  timer = setTimeout(() => void sync(current), 300)
  try {
    const url = `${deliveryUrl(props.taskId)}/${action}${action === 'verify' ? `?expected_generation_version=${state.value?.generation_version}` : ''}`
    const view = await requestMatch<DeliveryState>(url, {
      method: 'POST', ...(action === 'run' ? { headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_match_plan_version: props.matchPlanVersion, force }) } : {}),
    })
    if (current === generation) state.value = view
  } catch (caught) {
    if (current === generation) error.value = caught instanceof Error ? caught.message : '制表操作失败'
  } finally {
    if (current === generation) { busy.value = false; stop(); await sync(generation) }
  }
}
onMounted(() => void sync())
onBeforeUnmount(stop)
</script>

<template>
  <section class="delivery" aria-labelledby="delivery-title">
    <div class="heading"><div><p class="eyebrow">第三阶段 · 匹配版本 {{ matchPlanVersion }}</p><h2 id="delivery-title">制表、反向核查与交付</h2><p>生成 C 表和 B 核查副本，重新读取文件完成强制验收。</p></div><button v-if="!completed" class="btn btn-primary" type="button" :disabled="locked" @click="act('run')">{{ busy ? '正在处理…' : state?.status === 'running' ? '正在执行…' : state ? '重新生成并验收' : '生成文件并验收' }}</button></div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <template v-if="state">
      <p class="status" aria-live="polite">{{ deliveryLabels[state.status] }} · 生成版本 {{ state.generation_version }} · 第 {{ state.attempt }} 次执行</p>
      <details v-if="state.events.length" class="timeline"><summary>实际制表和核查记录 · {{ state.events.length }} 条</summary><ol><li v-for="event in state.events" :key="event.sequence"><time>{{ time(event.occurred_at) }}</time><span>{{ event.detail }}</span></li></ol></details>
      <p v-if="state.status === 'running' && !busy"><button class="btn btn-ghost" type="button" @click="act('run')">从保存位置恢复</button></p>
      <div v-if="result" class="statistics"><h3>本月 B 覆盖核查</h3><p>原始行 {{ result.statistics.b_current_month_original_rows }} 行 · 按方向拆分 {{ result.statistics.b_current_month_business_records }} 条</p><p class="equation">{{ result.statistics.b_current_month_business_records }} = 已承接 {{ result.statistics.taken_business_records }} + 已标注缺失 {{ result.statistics.missing_business_records }} + 人工排除 {{ result.statistics.excluded_business_records }}</p><p v-for="totals in result.statistics.totals" :key="totals.sheet_name">{{ totals.sheet_name }} · A/C 数量差额 {{ totals.quantity_difference }} · 金额差额 {{ totals.amount_difference }}</p></div>
      <ul v-if="report" class="checks" aria-label="实际文件强制验收结果"><li v-for="check in report.checks" :key="check.name"><span>{{ checkLabels[check.name] ?? check.name }}<small>{{ check.detail }}</small></span><strong :class="check.status">{{ checkStatus[check.status] }}</strong></li></ul>
      <ul v-if="state.issues.length" class="issues"><li v-for="(item, index) in state.issues" :key="index"><strong>{{ item.code }}</strong> {{ item.message }}<small>{{ item.output_location ?? item.a_record_id ?? item.b_record_id }} · 修复阶段 {{ item.stage === 'preparation' ? '数据准备' : item.stage === 'matching' ? '逐行匹配' : item.stage === 'input' ? '补充输入或验证能力' : '制表核查' }}</small></li></ul>
      <template v-if="completed && result"><h3 class="files-title">已通过验收的交付文件</h3><ul class="files"><li v-for="artifact in result.artifacts" :key="artifact.kind"><a :href="`${deliveryUrl(taskId)}/artifacts/${artifact.reference.name}?expected_generation_version=${state.generation_version}`" :download="artifact.reference.name">{{ deliveryFiles[artifact.kind] ?? artifact.reference.name }}<small>{{ artifact.reference.name }}</small></a></li></ul><p class="note">业务待复核项已保留在明细中。最终文件绑定当前数据、匹配及生成版本。</p></template>
      <details v-else-if="state.diagnostic_refs.length" class="diagnostics"><summary>下载诊断文件（尚未通过正式交付验收）</summary><ul><li v-for="artifact in state.diagnostic_refs" :key="artifact.relative_path"><a :href="`${deliveryUrl(taskId)}/diagnostics/${artifact.name}?expected_generation_version=${state.generation_version}`" :download="artifact.name">{{ artifact.name }}</a></li></ul></details>
      <div v-if="completed"><button class="btn btn-ghost" type="button" :disabled="locked" @click="act('verify')">重新验收实际文件</button><button class="btn btn-ghost" type="button" :disabled="locked" @click="act('run', true)">生成新版本</button></div>
    </template>
  </section>
</template>

<style scoped>
.delivery { margin-top: 30px; padding-top: 28px; border-top: 1px solid var(--border-strong); }
.heading { display: flex; align-items: center; justify-content: space-between; gap: 20px; }
h2 { font-size: 20px; } h3 { font-size: 15px; }
p { margin-top: 6px; color: var(--text-secondary); font-size: 13px; line-height: 1.7; }
.eyebrow, small, time { font-size: 11px; color: var(--text-tertiary); } .status { margin-block: 18px; color: var(--accent); }
.error, .issues { color: #9d302a; font-size: 12px; overflow-wrap: anywhere; } .issues { padding-left: 18px; margin-top: 16px; }
small { display: block; margin-top: 3px; line-height: 1.6; } .timeline { margin-top: 16px; font-size: 12px; }
summary { cursor: pointer; line-height: 1.7; } ol { padding-left: 18px; margin-top: 12px; } ol li { padding: 5px 0; } time { margin-right: 12px; }
.statistics { margin-top: 22px; } .equation { color: var(--accent); overflow-wrap: anywhere; }
.checks { list-style: none; margin-top: 20px; } .checks li { display: flex; align-items: flex-start; gap: 16px; padding: 11px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.checks li > span { flex: 1; } .checks strong { font-weight: 500; white-space: nowrap; } .pass { color: #24743e; } .fail { color: #9d302a; } .not_run { color: #8c580c; }
.files-title { margin-top: 24px; } .files { list-style: none; display: grid; grid-template-columns: 1fr 1fr; gap: 0 24px; }
.files a { display: block; padding: 13px 0; border-bottom: 1px solid var(--border); text-decoration: none; font-size: 13px; } a { color: var(--accent); }
.note { margin-block: 14px 6px; font-size: 12px; } .diagnostics { margin-top: 20px; color: #8c580c; font-size: 12px; } .diagnostics ul { padding: 10px 0 0 18px; }
@media (max-width: 560px) { .heading { flex-direction: column; align-items: flex-start; } .files { grid-template-columns: 1fr; } }
</style>
