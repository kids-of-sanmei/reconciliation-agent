<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import DeliveryPanel from './DeliveryPanel.vue'
import { matchUrl, MatchRequestError, requestMatch, ROW_LABELS, type Candidate, type MatchDecision, type MatchingState, type MatchResults } from '../lib/matching'

const props = defineProps<{ taskId: string }>()
const state = ref<MatchingState | null>(null)
const results = ref<MatchResults | null>(null)
const busy = ref(false)
const error = ref('')
const actor = ref('')
const reason = ref('')
const decision = ref<MatchDecision | null>(null)
const targetDescription = ref('')
const tolerance = ref(0)
const aliases = ref('')
const onlyReview = ref(true)
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
const candidates = computed(() => new Map(state.value?.progress.flatMap(row => row.candidates).map(c => [c.candidate_id, c]) ?? []))
const visibleRows = computed(() => results.value?.rows.filter(row => !onlyReview.value || row.status !== 'matched') ?? [])
const incomplete = computed(() => state.value?.progress.some(row => !row.search_complete))
const files = ['match_plan.json', 'a_row_results.json', 'match_groups.json', 'b_usage.json', 'review_items.json', 'exclusions.json', 'execution_issues.json', 'confirmations.json', 'progress.json']
const statusLabels = { running: '正在逐行匹配', ready: '匹配方案已保存', needs_input: '存在阻塞问题', failed: '匹配执行失败' }
const checkLabels: Record<string, string> = { identity: '商品与方向', quantity: '数量', amount: '金额', unit_price: '单价', date: '日期', spec: '规格', allocation: '分配', partial_amount: '已分配金额' }

function stop() { generation += 1; clearTimeout(timer) }
function candidateFor(id: string): Candidate | undefined { return candidates.value.get(id) }
function pick(next: MatchDecision, description: string) { decision.value = next; targetDescription.value = description; error.value = '' }

async function sync(current = generation) {
  try {
    const view = await requestMatch<MatchingState>(matchUrl(props.taskId))
    if (current !== generation) return
    state.value = view
    if (view.status === 'ready' || view.status === 'needs_input') {
      const data = await requestMatch<MatchResults>(`${matchUrl(props.taskId)}/results`)
      if (data.match_plan_version !== view.version) {
        if (current === generation) timer = setTimeout(() => void sync(current), 100)
        return
      }
      if (current === generation) results.value = data
    }
    if (current !== generation) return
    if (busy.value || view.status === 'running') timer = setTimeout(() => void sync(current), 800)
  } catch (caught) {
    if (current !== generation) return
    if (!(caught instanceof MatchRequestError && caught.status === 404 && !state.value)) {
      error.value = caught instanceof Error ? caught.message : '读取匹配进度失败'
    }
    if (busy.value || state.value?.status === 'running') timer = setTimeout(() => void sync(current), 2000)
  }
}

async function act(action: 'run' | 'search' | 'confirm', next?: MatchDecision) {
  if (busy.value) return
  stop()
  const current = generation
  busy.value = true
  error.value = ''
  const version = state.value?.version
  const body = action === 'confirm' ? {
    actor: actor.value, reason: reason.value, expected_match_plan_version: version, decision: next,
  } : undefined
  const url = `${matchUrl(props.taskId)}/${action}${action === 'search' ? `?expected_version=${version}` : ''}`
  timer = setTimeout(() => void sync(current), 300)
  try {
    const view = await requestMatch<MatchingState>(url, {
      method: 'POST', ...(body ? { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
    })
    if (current !== generation) return
    state.value = view
    if (action === 'confirm') { decision.value = null; reason.value = '' }
    tolerance.value = view.rules.date_tolerance_days
    aliases.value = Object.entries(view.rules.specification_aliases).map(([a, b]) => `${a} = ${b}`).join('\n')
  } catch (caught) {
    if (current === generation) error.value = caught instanceof Error ? caught.message : '匹配操作失败'
  } finally {
    if (current === generation) {
      busy.value = false
      stop()
      await sync(generation)
    }
  }
}

function proposeRules() {
  if (!state.value) return
  const mapping: Record<string, string> = {}
  for (const line of aliases.value.split('\n').filter(line => line.trim())) {
    const parts = line.split('=').map(part => part.trim())
    if (parts.length !== 2 || !parts[0] || !parts[1]) { error.value = '规格别名请每行填写“原名称 = 对应名称”'; return }
    mapping[parts[0]] = parts[1]
  }
  if (!Number.isInteger(tolerance.value) || tolerance.value < 0 || tolerance.value > 366) { error.value = '日期范围需为 0 至 366 天的整数'; return }
  pick({ action: 'rules', rules: { ...state.value.rules, version: `${state.value.rules.version}-r${state.value.version + 1}`,
    date_tolerance_days: tolerance.value, specification_aliases: mapping } }, '更新日期范围和规格别名，重新匹配全部 A 行')
}

onMounted(async () => {
  await sync()
  if (state.value) {
    tolerance.value = state.value.rules.date_tolerance_days
    aliases.value = Object.entries(state.value.rules.specification_aliases).map(([a, b]) => `${a} = ${b}`).join('\n')
  }
})
onBeforeUnmount(stop)
</script>

<template>
  <section class="matching" aria-labelledby="matching-title">
    <div class="heading"><div><p class="eyebrow">第二阶段</p><h2 id="matching-title">逐行匹配与复核</h2><p>核对对应关系并保存匹配方案，供下一阶段制表使用。</p></div>
      <button v-if="!state || state.status === 'failed'" type="button" class="btn btn-primary" :disabled="busy" @click="act('run')">{{ busy ? '正在执行…' : state ? '恢复匹配' : '开始逐行匹配' }}</button>
    </div>
    <p v-if="error" class="error" role="alert">{{ error }}</p>
    <template v-if="state">
      <p class="summary" aria-live="polite">{{ statusLabels[state.status] }} · 版本 {{ state.version }} · 已处理 {{ state.completed_rows }} / {{ state.total_rows }} 行</p>
      <progress v-if="busy || state.status === 'running'" :max="Math.max(1, state.total_rows)" :value="state.completed_rows" aria-label="已处理 A 行数" />
      <p v-for="issue in state.issues" :key="`${issue.code}-${issue.source_ref}`" class="error">{{ issue.source_ref }} {{ issue.message }}</p>
      <div v-if="incomplete" class="search-note"><p>部分组合搜索尚未完成，相关行保留待确认状态。继续搜索会从已保存的位置推进；达到组合或候选上限时，需要调整匹配规则。</p><button type="button" class="btn btn-ghost" :disabled="busy" @click="act('search')">继续未完成搜索</button></div>
      <p v-if="state.status === 'running' && !busy"><button type="button" class="btn btn-ghost" @click="act('run')">从已保存进度恢复</button></p>
      <template v-if="results && state.status !== 'running' && state.status !== 'failed'">
        <div class="rows-heading"><h3>A 行处理结果</h3><label><input v-model="onlyReview" type="checkbox" /> 只看需复核的行</label></div>
        <p v-if="!visibleRows.length">当前筛选下没有需展示的行。</p>
        <details v-for="row in visibleRows" :key="row.a_record_id" class="row">
          <summary><code>{{ row.a_record_id }}</code><span>{{ ROW_LABELS[row.status] }}</span><small>余量 {{ row.remaining_quantity ?? '不可计算' }} · 余款 {{ row.remaining_amount ?? '不可计算' }}</small></summary>
          <p>{{ row.reasons.join('；') }}</p>
          <button type="button" class="btn btn-ghost" :disabled="busy" @click="pick({ action: 'release', a_record_ids: [row.a_record_id] }, `释放 ${row.a_record_id} 的当前关系`)">释放当前关系</button>
          <details v-for="id in row.candidate_ids" :key="id" class="candidate">
            <summary>{{ candidateFor(id)?.eligible ? '可复核候选' : '未通过检查' }} · {{ candidateFor(id)?.b_record_ids.join(' + ') }}</summary>
            <template v-if="candidateFor(id)">
              <p>{{ candidateFor(id)?.review_reasons.join('；') }}</p>
              <div class="table-scroll"><table><thead><tr><th>检查项</th><th>A 口径</th><th>B 口径</th><th>真实差额</th><th>结果</th></tr></thead><tbody><tr v-for="(check, index) in candidateFor(id)?.checks" :key="`${check.field}-${index}`"><td>{{ checkLabels[check.field] ?? check.field }}</td><td>{{ check.expected ?? '缺失' }}</td><td>{{ check.actual ?? '缺失' }}</td><td>{{ check.delta ?? '—' }}</td><td>{{ check.passed ? '通过' : '需检查' }}</td></tr></tbody></table></div>
              <button type="button" class="btn btn-ghost" :disabled="busy || !candidateFor(id)?.eligible" @click="pick({ action: 'accept', candidate_id: id }, `确认 ${row.a_record_id} 对应 ${candidateFor(id)?.b_record_ids.join(' + ')}`)">选择此关系，填写确认依据</button>
            </template>
          </details>
          <p v-if="!row.candidate_ids.length">当前规则范围内没有候选。</p>
        </details>
        <details class="secondary"><summary>B 记录使用与业务排除</summary><p>业务排除需要确认人和具体原因。</p>
          <div v-for="usage in results.b_usage" :key="usage.b_record_id" class="usage"><code>{{ usage.b_record_id }}</code><span>{{ usage.excluded ? '已排除' : usage.group_ids.length ? '已承接' : '尚未承接' }} · 余量 {{ usage.remaining_quantity ?? '不可计算' }}</span><button type="button" class="btn btn-ghost" :disabled="busy" @click="pick({ action: usage.excluded ? 'restore_b' : 'exclude_b', b_record_ids: [usage.b_record_id] }, `${usage.excluded ? '恢复' : '业务排除'} ${usage.b_record_id}`)">{{ usage.excluded ? '恢复记录' : '业务排除' }}</button></div>
        </details>
        <details class="secondary"><summary>调整日期范围和规格别名</summary><label>允许日期差（天）<input v-model.number="tolerance" type="number" min="0" max="366" /></label><label>已确认规格别名（每行“原名称 = 对应名称”）<textarea v-model="aliases" rows="4" /></label><button type="button" class="btn btn-ghost" :disabled="busy" @click="proposeRules">填写本次规则变更依据</button></details>
        <form v-if="decision" class="confirmation" @submit.prevent="act('confirm', decision)"><h3>人工确认</h3><p>{{ targetDescription }}</p><label>确认人<input v-model="actor" required :disabled="busy" /></label><label>确认依据 / 业务原因<textarea v-model="reason" rows="3" required :disabled="busy" /></label><button class="btn btn-primary" type="submit" :disabled="busy || !actor.trim() || !reason.trim()">{{ busy ? '正在重新核对…' : '确认并生成新版本' }}</button><button class="btn btn-ghost" type="button" :disabled="busy" @click="decision = null">取消</button></form>
        <details class="secondary"><summary>下载版本 {{ state.version }} 匹配方案及复核记录</summary><ul class="downloads"><li v-for="name in files" :key="name"><a :href="`${matchUrl(taskId)}/artifacts/${name}?expected_version=${state.version}`" :download="name">{{ name }}</a></li></ul></details>
        <p class="scope">匹配方案和待复核事项将作为下方制表与反向核查的输入。</p>
        <DeliveryPanel v-if="state.status === 'ready'" :key="`${taskId}-${state.version}`" :task-id="taskId" :match-plan-version="state.version" />
      </template>
    </template>
  </section>
</template>

<style scoped>
.matching { margin-top: 36px; padding-top: 28px; border-top: 1px solid var(--border-strong); }
.heading, .rows-heading { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
h2 { font-size: 20px; } h3 { font-size: 15px; }
p, label, summary { font-size: 13px; line-height: 1.7; } p { color: var(--text-secondary); margin-top: 6px; }
.eyebrow, small { font-size: 11px; color: var(--text-tertiary); }
.summary { margin: 18px 0; color: var(--accent); } progress { width: 100%; }
.error { color: #9d302a; padding: 8px 0; overflow-wrap: anywhere; }
.search-note { padding: 12px 0; border-block: 1px solid var(--border); }
.rows-heading { margin-top: 24px; } .rows-heading label { display: flex; align-items: center; gap: 6px; }
details > summary { cursor: pointer; } .row { padding: 14px 0; border-bottom: 1px solid var(--border); }
.row > summary span { margin-left: 14px; color: var(--accent); } .row > summary small { display: block; margin-left: 16px; }
code { font-size: 11px; overflow-wrap: anywhere; } .candidate { margin: 8px 0 8px 16px; }
.candidate > summary { color: var(--text-secondary); overflow-wrap: anywhere; }
.table-scroll { overflow-x: auto; margin-top: 8px; } table { width: 100%; border-collapse: collapse; font-size: 11px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); } th { font-weight: 500; white-space: nowrap; }
.secondary { margin-top: 20px; } .secondary > label, .confirmation > label { display: grid; gap: 6px; margin-top: 12px; }
input:not([type='checkbox']), textarea { padding: 8px 10px; border: 1px solid var(--border-strong); border-radius: 5px; background: white; color: var(--text); font: inherit; max-width: 100%; }
.secondary input { max-width: 140px; } textarea { width: 100%; resize: vertical; }
.usage { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding-top: 8px; font-size: 12px; }
.usage span { flex: 1; color: var(--text-secondary); }
.confirmation { padding: 18px 0; margin-top: 20px; border-block: 1px solid var(--border-strong); }
.confirmation button { margin-top: 12px; } .downloads { padding: 10px 0 0 18px; font-size: 12px; }
a { color: var(--accent); } .scope { margin-top: 24px; font-size: 12px; }
@media (max-width: 560px) { .heading, .rows-heading { align-items: flex-start; flex-direction: column; } }
</style>
