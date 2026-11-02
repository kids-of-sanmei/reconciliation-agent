<script setup lang="ts">
import { computed } from 'vue'
import { Check, CircleAlert, Loader2 } from 'lucide-vue-next'
import type { TaskView } from '../lib/preparation'

const props = defineProps<{ task: TaskView }>()
const stages = ['接收文件', '解析工作簿', '识别字段', '标准化明细', '生成底稿与索引', '保存产物']
const position = computed(() => {
  const stage = props.task.stage
  if (stage === 'preparation_ready') return 6
  if (stage === 'publish_prepared') return 5
  if (['build_baseline', 'build_b_index'].includes(stage)) return 4
  if (['normalize_records', 'normalize_and_build_baseline', 'prepare'].includes(stage)) return 3
  if (['identify_fields', 'apply_confirmation', 'identify'].includes(stage)) return 2
  if (['parse_workbooks', 'parse'].includes(stage)) return 1
  return 0
})
function state(i: number) {
  if (props.task.status === 'ready' || i < position.value) return 'done'
  if (i !== position.value) return 'upcoming'
  return props.task.status === 'needs_input' ? 'waiting' : props.task.status === 'failed' ? 'failed' : 'active'
}
</script>

<template>
  <ol class="flow" aria-label="数据准备阶段">
    <li v-for="(stage, i) in stages" :key="stage" :class="state(i)" :aria-current="i === position ? 'step' : undefined">
      <span class="node" aria-hidden="true">
        <Check v-if="state(i) === 'done'" :size="15" />
        <CircleAlert v-else-if="['failed', 'waiting'].includes(state(i))" :size="15" />
        <Loader2 v-else-if="state(i) === 'active'" :size="15" class="spin" />
        <span v-else>{{ i + 1 }}</span>
      </span>
      <span>{{ stage }}</span>
    </li>
  </ol>
</template>

<style scoped>
.flow { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; list-style: none; padding: 24px 0; border-block: 1px solid var(--border); }
li { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-tertiary); }
.node { display: grid; place-items: center; flex: 0 0 26px; height: 26px; border-radius: 50%; border: 1px solid var(--border-strong); font-variant-numeric: tabular-nums; }
.done .node { background: #e3f2e8; color: #24743e; border-color: transparent; }
.active { color: var(--accent); font-weight: 600; }
.active .node { background: var(--accent-soft); border-color: transparent; }
.waiting { color: #8c580c; }
.failed { color: #bd342e; }
.spin { animation: spin 1s linear infinite; }
@media (max-width: 780px) { .flow { grid-template-columns: repeat(3, 1fr); row-gap: 18px; } }
@media (max-width: 400px) { .flow { grid-template-columns: repeat(2, 1fr); } }
</style>
