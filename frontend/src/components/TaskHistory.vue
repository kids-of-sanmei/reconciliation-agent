<script setup lang="ts">
import { computed, ref } from 'vue'
import { Check, CircleAlert, Clock3, Download, Loader2 } from 'lucide-vue-next'
import { STAGE_LABELS, type TaskView } from '../lib/preparation'
import { saveBlob } from '../lib/download'

const props = defineProps<{ task: TaskView }>()
const onlyProblems = ref(false)
const events = computed(() => onlyProblems.value
  ? props.task.events.filter((event) => ['waiting', 'failed'].includes(event.kind))
  : props.task.events)
const clock = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
})
const kindLabels = { started: '开始', completed: '完成', info: '说明', waiting: '待补充', failed: '失败' }

function downloadHistory() {
  saveBlob(new Blob([JSON.stringify(props.task, null, 2)], { type: 'application/json' }),
    `执行记录_${props.task.task_id}.json`)
}
</script>

<template>
  <section class="history" aria-labelledby="history-title">
    <header>
      <div><h3 id="history-title">执行记录 <span>{{ task.events.length }}</span></h3><p>按实际发生时间保存 · 北京时间</p></div>
      <button class="text-button" type="button" @click="downloadHistory"><Download :size="14" aria-hidden="true" />下载记录</button>
    </header>
    <label v-if="task.events.length" class="filter"><input v-model="onlyProblems" type="checkbox" />只看暂停和失败</label>
    <ol v-if="events.length" aria-label="实际执行动作">
      <li v-for="event in events" :key="event.sequence" :class="`event-${event.kind}`">
        <div class="event-time"><time :datetime="event.occurred_at">{{ clock.format(new Date(event.occurred_at)) }}</time><small>第 {{ event.sequence }} 条 · 版本 {{ event.data_version }}</small></div>
        <span class="event-dot" aria-hidden="true">
          <Check v-if="event.kind === 'completed'" :size="13" />
          <CircleAlert v-else-if="['waiting', 'failed'].includes(event.kind)" :size="13" />
          <Loader2 v-else-if="event.kind === 'started' && event.sequence === task.events.length && !['ready', 'failed', 'needs_input'].includes(task.status)" :size="13" class="spin" />
          <Clock3 v-else :size="12" />
        </span>
        <div class="event-content">
          <div class="event-heading"><h4>{{ event.title }}</h4><span class="kind">{{ kindLabels[event.kind] }}</span></div>
          <p v-if="event.detail">{{ event.detail }}</p>
          <small class="stage">{{ STAGE_LABELS[event.stage] ?? event.stage }}</small>
        </div>
      </li>
    </ol>
    <p v-else class="empty">{{ onlyProblems ? '当前没有暂停或失败记录。' : '这个任务还没有执行记录。旧任务的过往动作不会补记，后续执行将开始记录。' }}</p>
  </section>
</template>

<style scoped>
.history { margin-top: 32px; }
header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
h3 { font-size: 17px; font-weight: 600; }
h3 span { font-size: 12px; font-weight: 500; color: var(--text-tertiary); margin-left: 5px; font-variant-numeric: tabular-nums; }
header p { font-size: 12px; color: var(--text-tertiary); margin-top: 3px; }
.text-button { display: inline-flex; align-items: center; gap: 5px; border: 0; background: transparent; font: inherit; font-size: 12px; color: var(--accent); cursor: pointer; padding: 8px 0; }
.filter { display: inline-flex; align-items: center; gap: 7px; color: var(--text-secondary); font-size: 12px; margin: 18px 0 22px; cursor: pointer; }
input { accent-color: var(--accent); }
ol { list-style: none; }
li { display: grid; grid-template-columns: 126px 24px minmax(0, 1fr); gap: 14px; position: relative; padding-bottom: 24px; }
.event-time { font-variant-numeric: tabular-nums; font-size: 12px; padding-top: 2px; color: var(--text-secondary); }
.event-time small { display: block; font-size: 10px; color: var(--text-tertiary); margin-top: 3px; }
.event-dot { position: relative; display: grid; place-items: center; width: 24px; height: 24px; border-radius: 50%; background: #edf1f4; color: var(--text-secondary); }
li:not(:last-child)::after { content: ''; position: absolute; left: 151px; top: 28px; bottom: 4px; width: 1px; background: var(--border-strong); }
.event-completed .event-dot { color: #24743e; background: #e3f2e8; }
.event-started .event-dot { color: var(--accent); background: #e5effc; }
.event-waiting .event-dot { color: #8c580c; background: #fbecd5; }
.event-failed .event-dot { color: #bd342e; background: #fbe6e4; }
.event-heading { display: flex; align-items: baseline; gap: 10px; }
h4 { font-size: 14px; font-weight: 500; overflow-wrap: anywhere; }
.kind { font-size: 10px; color: var(--text-tertiary); white-space: nowrap; }
.event-waiting h4 { color: #8c580c; }
.event-failed h4 { color: #bd342e; }
.event-content p { color: var(--text-secondary); font-size: 13px; margin-top: 4px; overflow-wrap: anywhere; }
.stage { display: inline-block; color: var(--text-tertiary); font-size: 11px; margin-top: 5px; }
.empty { padding: 24px 0; font-size: 13px; color: var(--text-secondary); }
.spin { animation: spin 1s linear infinite; }
@media (max-width: 560px) {
  li { grid-template-columns: 24px minmax(0, 1fr); gap: 5px 12px; }
  .event-dot { grid-column: 1; grid-row: 1 / 3; }
  .event-time { grid-column: 2; display: flex; align-items: center; gap: 8px; padding: 0; }
  .event-time small { margin: 0; }
  .event-content { grid-column: 2; }
  li:not(:last-child)::after { left: 11px; }
}
</style>
