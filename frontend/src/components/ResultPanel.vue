<script setup lang="ts">
import { computed } from 'vue'
import { CheckCircle2, Download, RotateCcw } from 'lucide-vue-next'
import type { ReconcileResult } from '../composables/useReconcile'
import { saveBlob } from '../lib/download'

const props = defineProps<{
  result: ReconcileResult
  isDemo: boolean
}>()

const emit = defineEmits<{
  reset: []
}>()

const statItems = computed(() =>
  props.result.stats
    ? [
        { label: '完全一致', value: props.result.stats.matched, tone: 'success' },
        { label: '内容不一致', value: props.result.stats.mismatched, tone: 'warning' },
        { label: '仅账单 A 存在', value: props.result.stats.onlyA, tone: 'neutral' },
        { label: '仅账单 B 存在', value: props.result.stats.onlyB, tone: 'neutral' },
      ]
    : [],
)

function download() {
  saveBlob(props.result.blob, props.result.filename)
}
</script>

<template>
  <div class="material-panel panel" role="status" aria-live="polite">
    <span class="badge-success" aria-hidden="true">
      <CheckCircle2 :size="28" :stroke-width="2" />
    </span>

    <h3 class="panel-title">对账完成</h3>
    <p class="panel-sub">
      {{ result.filename }}
      <span v-if="isDemo" class="demo-tag">
        <span class="dot" aria-hidden="true" />
        演示模式 · 本地生成
      </span>
    </p>

    <div v-if="statItems.length" class="stats">
      <div v-for="s in statItems" :key="s.label" class="stat">
        <span class="stat-value" :class="`tone-${s.tone}`">{{ s.value }}</span>
        <span class="stat-label">{{ s.label }}</span>
      </div>
    </div>

    <div class="actions">
      <button class="btn btn-primary" type="button" @click="download">
        <Download :size="16" :stroke-width="2.2" aria-hidden="true" />
        下载对账报表
      </button>
      <button class="btn btn-ghost" type="button" @click="emit('reset')">
        <RotateCcw :size="15" :stroke-width="2.2" aria-hidden="true" />
        再来一单
      </button>
    </div>
  </div>
</template>

<style scoped>
.panel {
  padding: 44px 40px;
  text-align: center;
}

.badge-success {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: rgb(52 199 89 / 12%);
  color: var(--success);
  margin-bottom: 18px;
}

.panel-title {
  font-size: 24px;
  font-weight: 600;
  letter-spacing: 0;
  margin-bottom: 8px;
}

.panel-sub {
  font-size: 14px;
  color: var(--text-secondary);
  margin-bottom: 30px;
  word-break: break-all;
}

/* 胶囊标签：圆点 + 完整文字 */
.demo-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: 8px;
  font-size: 12px;
  font-weight: 500;
  color: var(--warning);
  background: rgb(255 149 0 / 10%);
  border-radius: 980px;
  padding: 3px 10px;
  vertical-align: 1px;
}

.demo-tag .dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--warning);
}

.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  max-width: 560px;
  margin: 0 auto 32px;
}

.stat {
  background: var(--surface-alt);
  border-radius: var(--radius-panel);
  padding: 14px 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.stat-value {
  font-size: 24px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

.tone-success {
  color: var(--success);
}

.tone-warning {
  color: var(--warning);
}

.tone-neutral {
  color: var(--text);
}

.stat-label {
  font-size: 12px;
  color: var(--text-tertiary);
}

.actions {
  display: flex;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
}

@media (max-width: 560px) {
  .panel {
    padding: 36px 20px;
  }

  .stats {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
