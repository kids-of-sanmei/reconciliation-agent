<script setup lang="ts">
import { ref, watch } from 'vue'
import type { ConfirmationInput, TaskOptions, TaskView } from '../lib/preparation'

const props = defineProps<{ task: TaskView; busy: boolean }>()
const emit = defineEmits<{ confirm: [input: ConfirmationInput] }>()
const actor = ref('')
const reason = ref('')
const period = ref('')
const optionsText = ref('')
const formulaText = ref('{}')
const advanced = ref(false)
const error = ref('')

watch(() => props.task.data_version, () => {
  period.value = props.task.options.reconciliation_period ?? ''
  optionsText.value = JSON.stringify(props.task.options, null, 2)
}, { immediate: true })

function submit() {
  error.value = ''
  try {
    const options: TaskOptions = advanced.value ? JSON.parse(optionsText.value) : { ...props.task.options }
    if (!options || typeof options !== 'object' || Array.isArray(options)) throw new Error('字段配置必须是 JSON 对象')
    options.reconciliation_period = period.value || null
    const formulaValues: unknown = advanced.value ? JSON.parse(formulaText.value) : {}
    if (!formulaValues || typeof formulaValues !== 'object' || Array.isArray(formulaValues)
      || Object.values(formulaValues).some((value) => typeof value !== 'string')) {
      throw new Error('公式核实结果必须是键值为文本的 JSON 对象')
    }
    emit('confirm', {
      actor: actor.value.trim(), reason: reason.value.trim(), options,
      formula_values: formulaValues as Record<string, string>, expected_data_version: props.task.data_version,
    })
  } catch (err) {
    error.value = err instanceof SyntaxError ? 'JSON 格式有误，请检查字段配置或公式核实结果。'
      : err instanceof Error ? err.message : '补充信息格式有误'
  }
}
</script>

<template>
  <section class="confirmation" aria-labelledby="confirmation-title">
    <h3 id="confirmation-title">补充信息后继续</h3>
    <p>当前处理已暂停。请核实下面的问题，补充内容会随人员和时间保存。</p>
    <ul><li v-for="(issue, i) in task.issues" :key="i"><span>{{ issue.message }}</span><small v-if="issue.source_ref">来源：{{ issue.source_ref }}</small></li></ul>
    <form @submit.prevent="submit">
      <div class="fields">
        <label>对账月份<input v-model="period" type="month" min="0001-01" max="9999-12" required :disabled="busy" /></label>
        <label>确认人<input v-model="actor" type="text" required maxlength="100" autocomplete="name" :disabled="busy" placeholder="填写姓名" /></label>
      </div>
      <label>确认说明<input v-model="reason" type="text" required maxlength="1000" :disabled="busy" placeholder="说明核实或调整的内容" /></label>
      <details @toggle="advanced = ($event.target as HTMLDetailsElement).open">
        <summary>字段映射或公式结果需要补充</summary>
        <p>月份以上方输入为准。保留现有配置，在 mappings 中补充工作表、行范围和字段列标；公式结果以问题给出的来源编号为键，数值填写为文本。</p>
        <label>完整字段配置（JSON）<textarea v-model="optionsText" rows="12" spellcheck="false" :disabled="busy" /></label>
        <label>公式核实结果（JSON）<textarea v-model="formulaText" rows="3" spellcheck="false" :disabled="busy" /></label>
      </details>
      <p v-if="error" class="form-error" role="alert">{{ error }}</p>
      <button class="btn btn-primary" type="submit" :disabled="busy || !actor.trim() || !reason.trim()">{{ busy ? '正在恢复执行…' : '保存补充并继续' }}</button>
    </form>
  </section>
</template>

<style scoped>
.confirmation { margin-top: 28px; padding: 24px; border-left: 3px solid #c18b32; border-radius: 4px; background: #fff8ec; }
h3 { font-size: 17px; font-weight: 600; }
p, li { font-size: 13px; color: var(--text-secondary); }
p { margin-top: 5px; }
ul { margin: 14px 0 18px; padding-left: 18px; }
li { margin: 6px 0; }
li small { display: block; overflow-wrap: anywhere; }
.fields { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
label { display: flex; flex-direction: column; gap: 6px; font-size: 12px; margin-bottom: 14px; }
input, textarea { width: 100%; padding: 10px 12px; border: 1px solid var(--border-strong); border-radius: 6px; background: #fff; color: var(--text); font: inherit; font-size: 14px; }
textarea { font-family: Consolas, monospace; font-size: 12px; resize: vertical; }
details { margin: 12px 0 20px; }
summary { font-size: 12px; color: var(--accent); cursor: pointer; }
details p { margin-bottom: 12px; }
.form-error { color: #bd342e; margin: 10px 0; }
@media (max-width: 560px) { .confirmation { padding: 18px; } .fields { grid-template-columns: 1fr; gap: 0; } }
</style>
