<script setup lang="ts">
import { computed } from 'vue'
import { Check, Loader2 } from 'lucide-vue-next'
import type { Phase } from '../composables/useReconcile'

const props = defineProps<{
  stages: string[]
  stageIndex: number
  phase: Phase
}>()

function stateOf(i: number): 'done' | 'active' | 'upcoming' {
  if (props.phase === 'success' || i < props.stageIndex) return 'done'
  if (i === props.stageIndex) return 'active'
  return 'upcoming'
}

const progressPercent = computed(() => {
  const total = props.stages.length
  const done = props.phase === 'success' ? total : props.stageIndex
  return Math.min(100, (done / total) * 100)
})
</script>

<template>
  <div class="flow" aria-live="polite" aria-label="对账进度">
    <ol class="steps">
      <li v-for="(stage, i) in stages" :key="stage" class="step" :class="`is-${stateOf(i)}`">
        <span class="step-node" aria-hidden="true">
          <Check v-if="stateOf(i) === 'done'" :size="14" :stroke-width="2.6" />
          <Loader2 v-else-if="stateOf(i) === 'active'" :size="14" :stroke-width="2.4" class="spin" />
          <span v-else class="step-num">{{ i + 1 }}</span>
        </span>
        <span class="step-label">{{ stage }}</span>
        <span v-if="i < stages.length - 1" class="step-line" aria-hidden="true">
          <span class="step-line-fill" :class="{ filled: stateOf(i) === 'done' }" />
        </span>
      </li>
    </ol>

    <div class="track" role="progressbar" :aria-valuenow="progressPercent" aria-valuemin="0" aria-valuemax="100">
      <div class="track-fill" :style="{ width: `${progressPercent}%` }" />
    </div>
    <p class="flow-hint">
      {{ phase === 'success' ? '报表已生成' : `正在${stages[Math.min(stageIndex, stages.length - 1)]}…` }}
    </p>
  </div>
</template>

<style scoped>
.flow {
  padding: 8px 4px;
}

.steps {
  display: flex;
  align-items: center;
  list-style: none;
  margin-bottom: 20px;
}

.step {
  display: flex;
  align-items: center;
  gap: 10px;
}

.step:not(:last-child) {
  flex: 1;
}

.step-node {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 50%;
  font-size: 12px;
  font-weight: 600;
  transition:
    background-color var(--duration-normal) ease,
    color var(--duration-normal) ease,
    border-color var(--duration-normal) ease;
}

.step.is-done .step-node {
  background: rgb(52 199 89 / 14%);
  color: var(--success);
}

.step.is-active .step-node {
  background: var(--primary);
  color: #fff;
}

.step.is-upcoming .step-node {
  border: 1.5px solid var(--border-strong);
  color: var(--text-tertiary);
}

.spin {
  animation: spin 0.8s linear infinite;
}

.step-label {
  font-size: 14px;
  white-space: nowrap;
  transition: color var(--duration-normal) ease;
}

.step.is-done .step-label {
  color: var(--text-secondary);
}

.step.is-active .step-label {
  color: var(--text);
  font-weight: 600;
}

.step.is-upcoming .step-label {
  color: var(--text-tertiary);
}

.step-line {
  flex: 1;
  height: 1.5px;
  margin: 0 14px;
  background: var(--border);
  border-radius: 2px;
  overflow: hidden;
}

.step-line-fill {
  display: block;
  height: 100%;
  width: 0;
  background: var(--success);
  transition: width var(--duration-normal) var(--ease-out);
}

.step-line-fill.filled {
  width: 100%;
}

.track {
  height: 4px;
  border-radius: 4px;
  background: var(--border);
  overflow: hidden;
}

.track-fill {
  height: 100%;
  background: var(--primary);
  border-radius: 4px;
  transition: width 0.5s var(--ease-out);
}

.flow-hint {
  margin-top: 12px;
  font-size: 13px;
  color: var(--text-tertiary);
  text-align: center;
}

@media (max-width: 560px) {
  .step-label {
    display: none;
  }

  .step.is-active .step-label {
    display: inline;
  }
}
</style>
