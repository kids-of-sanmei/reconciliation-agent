<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowLeftRight, CircleAlert, Loader2, RefreshCw } from 'lucide-vue-next'
import UploadCard from '../components/UploadCard.vue'
import ProgressFlow from '../components/ProgressFlow.vue'
import ResultPanel from '../components/ResultPanel.vue'
import TaskHistory from '../components/TaskHistory.vue'
import TaskConfirmation from '../components/TaskConfirmation.vue'
import MatchingPanel from '../components/MatchingPanel.vue'
import { useReconcile } from '../composables/useReconcile'
import { STAGE_LABELS, STATUS_LABELS } from '../lib/preparation'

const fileA = ref<File | null>(null)
const fileB = ref<File | null>(null)
const period = ref('')
const restoreId = ref('')
const { task, errorMsg, submitting, syncing, processing, lastSyncedAt, start, loadTask, refresh, retry, confirm, reset } = useReconcile()
const canStart = computed(() => !!fileA.value && !!fileB.value && !submitting.value)
const time = (value: string) => new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
}).format(new Date(value))

function onStart() {
  if (fileA.value && fileB.value) void start(fileA.value, fileB.value, period.value)
}
function onReset() {
  fileA.value = null
  fileB.value = null
  period.value = ''
  reset()
}
</script>

<template>
  <main class="page">
    <div class="page-inner">
      <header class="masthead">
        <div class="brand"><ArrowLeftRight :size="21" :stroke-width="2" aria-hidden="true" /><span>清账</span><span class="brand-context">数据准备 · 匹配 · 核查</span></div>
        <button v-if="task" class="btn btn-ghost" type="button" :disabled="submitting" @click="onReset">新建任务</button>
      </header>

      <section v-if="!task" class="intake" aria-labelledby="intake-title">
        <p class="eyebrow">第一阶段</p>
        <h1 id="intake-title">上传账表，查看真实处理过程</h1>
        <p class="intro">从文件解析到生成 C 初始底稿，每个动作都会留下时间和结果。</p>
        <div class="upload-grid">
          <UploadCard v-model="fileA" label="A 表 · 业务底稿" hint="选择客户或供应商的对账底稿" :disabled="submitting" />
          <UploadCard v-model="fileB" label="B 表 · 系统出入库明细" hint="选择系统导出的出入库账表" :disabled="submitting" />
        </div>
        <form class="start-form" @submit.prevent="onStart">
          <label class="period-field">对账月份 <span>留空自动识别</span><input v-model="period" type="month" min="0001-01" max="9999-12" :disabled="submitting" /></label>
          <button class="btn btn-primary" type="submit" :disabled="!canStart"><Loader2 v-if="submitting" :size="16" class="spin" aria-hidden="true" />{{ submitting ? '正在接入任务…' : '开始数据准备' }}</button>
        </form>
        <p class="scope-note">当前支持 .xlsx，单文件最大 20 MB。正式匹配与最终核查将在后续阶段执行。</p>
      </section>

      <div v-if="errorMsg" class="request-error" role="alert">
        <CircleAlert :size="18" aria-hidden="true" /><p>{{ errorMsg }}<small v-if="task">页面保留最后一次获取的真实状态。</small></p>
        <button class="btn btn-ghost" type="button" :disabled="submitting" @click="task ? refresh() : onReset()">{{ task ? '重新查询' : '清除提示' }}</button>
      </div>

      <section v-if="task" class="task-panel" aria-labelledby="task-title">
        <div class="task-heading">
          <div aria-live="polite">
            <p class="eyebrow">第一阶段 · 版本 {{ task.data_version }}</p>
            <h1 id="task-title" :class="`status-${task.status}`">{{ STATUS_LABELS[task.status] }}</h1>
            <p class="current-stage">{{ submitting ? '正在提交并执行，请稍候…' : `当前阶段：${STAGE_LABELS[task.stage] ?? task.stage}` }}</p>
          </div>
          <button class="refresh-button" type="button" :disabled="submitting || syncing" aria-label="刷新任务状态" @click="refresh"><RefreshCw :size="17" :class="{ spin: syncing }" aria-hidden="true" /></button>
        </div>
        <div class="task-meta">
          <p>任务编号 <code>{{ task.task_id }}</code></p>
          <p v-for="source in task.sources" :key="source.role">{{ source.role === 'a' ? 'A 表' : source.role === 'b' ? 'B 表' : '参考表' }} <span>{{ source.original_name }}</span></p>
          <p v-if="task.options.reconciliation_period">对账月份 <span>{{ task.options.reconciliation_period }}</span></p>
          <p v-if="lastSyncedAt" class="sync-note">{{ processing ? '自动更新中' : '最近同步' }} · {{ time(lastSyncedAt) }}<span>刷新页面可继续查看此任务</span></p>
        </div>
        <ProgressFlow :task="task" />
        <TaskConfirmation v-if="task.status === 'needs_input'" :task="task" :busy="submitting" @confirm="confirm" />
        <div v-if="task.status === 'failed'" class="failure" role="status">
          <h3>处理停在当前阶段</h3>
          <p v-for="(issue, i) in task.issues" :key="i">{{ issue.message }}</p>
          <button class="btn btn-primary" type="button" :disabled="submitting" @click="retry">{{ submitting ? '正在重试…' : '重试当前任务' }}</button>
        </div>
        <details v-if="processing && !submitting" class="recovery">
          <summary>进度长时间没有变化？</summary>
          <p>如后端服务曾重启，可从已保存的执行位置恢复；任务正在运行时会提示稍后查询。</p>
          <button class="btn btn-ghost" type="button" @click="retry">恢复执行</button>
        </details>
        <TaskHistory :task="task" />
        <ResultPanel v-if="task.status === 'ready'" :task="task" />
        <MatchingPanel v-if="task.status === 'ready'" :key="task.task_id" :task-id="task.task_id" />
        <ul v-if="task.status === 'ready' && task.issues.length" class="warnings"><li v-for="(issue, i) in task.issues" :key="i">{{ issue.message }}</li></ul>
      </section>

      <details v-if="!task" class="restore">
        <summary>查看已有任务的执行记录</summary>
        <form @submit.prevent="loadTask(restoreId)"><label for="restore-id">任务编号</label><div><input id="restore-id" v-model="restoreId" type="text" required placeholder="粘贴任务编号" :disabled="submitting" /><button class="btn btn-ghost" type="submit" :disabled="submitting || !restoreId.trim()">查看记录</button></div></form>
      </details>
    </div>
  </main>
</template>

<style scoped>
.page { min-height: 100dvh; padding: 40px 28px 64px; }
.page-inner { width: 100%; max-width: 1000px; margin-inline: auto; }
.masthead { display: flex; justify-content: space-between; align-items: center; min-height: 44px; margin-bottom: 48px; }
.brand { display: flex; align-items: center; gap: 10px; color: var(--accent); font-size: 18px; font-weight: 600; }
.brand-context { color: var(--text-secondary); font-size: 12px; font-weight: 400; margin-left: 8px; padding-left: 16px; border-left: 1px solid var(--border-strong); }
.intake { padding: 28px 0 12px; }
.eyebrow { color: var(--text-tertiary); font-size: 12px; margin-bottom: 8px; }
h1 { font-size: clamp(24px, 3vw, 32px); font-weight: 600; line-height: 1.35; text-wrap: balance; }
.intro { font-size: 14px; color: var(--text-secondary); margin-top: 12px; }
.upload-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 28px; margin-top: 40px; }
.start-form { display: flex; align-items: flex-end; justify-content: space-between; gap: 20px; margin-top: 18px; }
.period-field { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; width: 250px; font-size: 13px; }
.period-field span { font-size: 11px; color: var(--text-tertiary); }
input { min-width: 0; padding: 10px 12px; border: 1px solid var(--border-strong); border-radius: 6px; font: inherit; background: rgb(255 255 255 / 80%); color: var(--text); }
.period-field input { width: 100%; }
.scope-note { margin-top: 22px; font-size: 12px; color: var(--text-tertiary); }
.request-error { display: flex; align-items: center; gap: 12px; background: #fff0ee; color: #9d302a; padding: 16px 20px; border-radius: 8px; margin: 24px 0; }
.request-error p { flex: 1; font-size: 13px; }
.request-error small { display: block; font-size: 11px; margin-top: 3px; }
.task-panel { background: rgb(255 255 255 / 80%); border: 1px solid rgb(255 255 255 / 85%); border-radius: 8px; padding: 32px; }
.task-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.current-stage { font-size: 13px; color: var(--text-secondary); margin-top: 8px; }
.status-needs_input { color: #8c580c; }
.status-failed { color: #bd342e; }
.status-ready { color: #24743e; }
.refresh-button { display: grid; place-items: center; padding: 10px; color: var(--accent); border: 1px solid var(--border); border-radius: 6px; background: transparent; cursor: pointer; }
.refresh-button:disabled { opacity: .5; cursor: wait; }
.task-meta { display: grid; grid-template-columns: 1fr 1fr; gap: 7px 24px; margin: 22px 0 20px; font-size: 12px; color: var(--text-tertiary); }
.task-meta p:first-child, .sync-note { grid-column: 1 / -1; }
.task-meta code { margin-left: 8px; color: var(--text-secondary); font-size: 11px; user-select: all; overflow-wrap: anywhere; }
.task-meta span { color: var(--text-secondary); margin-left: 8px; overflow-wrap: anywhere; }
.task-meta .sync-note span { color: var(--text-tertiary); }
.failure { margin-top: 28px; padding: 22px; background: #fff0ee; border-radius: 6px; }
.failure h3 { font-size: 16px; }
.failure p { margin: 8px 0; color: #9d302a; font-size: 13px; }
.failure button { margin-top: 10px; }
.recovery, .restore { color: var(--text-secondary); font-size: 12px; margin-top: 24px; }
summary { cursor: pointer; }
.recovery p { margin-top: 10px; }
.restore form { margin-top: 14px; max-width: 520px; }
.restore form div { display: flex; gap: 12px; margin-top: 6px; }
.restore input { flex: 1; width: 100%; }
.warnings { margin-top: 24px; padding-left: 18px; font-size: 12px; color: #8c580c; }
.spin { animation: spin 1s linear infinite; }
@media (max-width: 700px) {
  .page { padding: 24px 16px 40px; }
  .masthead { margin-bottom: 24px; }
  .brand-context { display: none; }
  .upload-grid { grid-template-columns: 1fr; gap: 8px; margin-top: 28px; }
  .start-form { align-items: stretch; flex-direction: column; }
  .period-field { width: 100%; }
  .task-panel { padding: 22px 18px; }
  .task-meta { grid-template-columns: 1fr; }
  .sync-note span { display: block; margin-left: 0; margin-top: 4px; }
  .request-error { flex-wrap: wrap; }
}
</style>
