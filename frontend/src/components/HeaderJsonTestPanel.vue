<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import { Download, Loader2 } from 'lucide-vue-next'
import { generateHeaderJson, type HeaderSelection } from '../lib/headers'
import { saveBlob } from '../lib/download'

const props = defineProps<{
  fileA: File | null
  fileB: File | null
  disabled?: boolean
}>()
const emit = defineEmits<{ 'busy-change': [busy: boolean] }>()

type Role = 'a' | 'b'
interface TestResult {
  role: Role
  filename: string
  status: 'loading' | 'ready' | 'failed'
  json: string
  error: string
}

const busy = ref(false)
const results = ref<TestResult[]>([])
const ranges = reactive<Record<Role, HeaderSelection>>({
  a: { start: '', count: '' }, b: { start: '', count: '' },
})
const sources = computed(() => {
  const files: { role: Role; file: File }[] = []
  if (props.fileA) files.push({ role: 'a', file: props.fileA })
  if (props.fileB) files.push({ role: 'b', file: props.fileB })
  return files
})

watch([
  () => props.fileA, () => props.fileB,
  () => ranges.a.start, () => ranges.a.count, () => ranges.b.start, () => ranges.b.count,
], () => { results.value = [] })

async function generate() {
  if (busy.value || props.disabled || !sources.value.length) return
  const selected = sources.value.map(source => ({ ...source, selection: { ...ranges[source.role] } }))
  results.value = selected.map(({ role, file }) => ({
    role, filename: file.name, status: 'loading', json: '', error: '',
  }))
  busy.value = true
  emit('busy-change', true)
  try {
    await Promise.all(selected.map(async ({ file, selection }, index) => {
      const result = results.value[index]
      try {
        result.json = await generateHeaderJson(file, selection)
        result.status = 'ready'
      } catch (error) {
        result.error = error instanceof Error ? error.message : '生成 JSON 失败，请重试。'
        result.status = 'failed'
      }
    }))
  } finally {
    busy.value = false
    emit('busy-change', false)
  }
}

function download(result: TestResult) {
  const name = result.filename.replace(/\.(xlsx|xls)$/i, '')
  saveBlob(
    new Blob([result.json], { type: 'application/json;charset=utf-8' }),
    `${result.role.toUpperCase()}-${name}.headers.json`,
  )
}
</script>

<template>
  <section class="header-test" aria-labelledby="header-test-title" :aria-busy="busy">
    <div class="test-heading">
      <h2 id="header-test-title">表头 JSON 测试</h2>
      <p>使用上方选择的 Excel，生成表头结构并查看合并单元格的展开结果。可只选一张表。</p>
    </div>
    <form @submit.prevent="generate">
      <details v-if="sources.length" class="range-options">
        <summary>指定表头范围（可选）</summary>
        <p>留空从首个非空行读取，并展开跨行合并。有标题或多行表头时，可分别指定范围。</p>
        <div class="range-grid">
          <fieldset v-for="source in sources" :key="source.role" :disabled="busy || disabled">
            <legend>{{ source.role.toUpperCase() }} 表</legend>
            <label>表头起始行<input v-model="ranges[source.role].start" type="number" min="1" step="1" placeholder="自动" /></label>
            <label>表头行数<input v-model="ranges[source.role].count" type="number" min="1" step="1" placeholder="1（覆盖跨行合并）" /></label>
          </fieldset>
        </div>
      </details>
      <button class="btn btn-primary" type="submit" :disabled="busy || disabled || !sources.length">
        <Loader2 v-if="busy" :size="16" class="spin" aria-hidden="true" />
        {{ busy ? '正在生成 JSON…' : '生成表头 JSON' }}
      </button>
      <p class="test-note">支持 .xlsx / .xls，JSON 只包含表头，合并区域填充相同内容。</p>
    </form>
    <div class="test-results" aria-live="polite">
      <article v-for="result in results" :key="result.role" class="test-result">
        <div class="result-heading">
          <h3>{{ result.role.toUpperCase() }} 表 · {{ result.filename }}</h3>
          <button v-if="result.status === 'ready'" class="btn btn-ghost" type="button" @click="download(result)">
            <Download :size="16" aria-hidden="true" />下载 JSON
          </button>
        </div>
        <p v-if="result.status === 'loading'" class="result-loading">正在上传并生成表头 JSON…</p>
        <p v-else-if="result.status === 'failed'" class="result-error" role="alert">{{ result.error }}</p>
        <details v-else open class="json-preview">
          <summary>预览 JSON</summary>
          <pre tabindex="0" :aria-label="`${result.role.toUpperCase()} 表的 JSON 预览`">{{ result.json }}</pre>
        </details>
      </article>
    </div>
  </section>
</template>

<style scoped>
.header-test { margin-top: 10px; padding: 24px 0; border-block: 1px solid var(--border-strong); }
.test-heading h2 { font-size: 17px; font-weight: 600; }
.test-heading p, .test-note, .range-options p { font-size: 12px; color: var(--text-secondary); margin-top: 6px; }
form { margin-top: 18px; }
.range-options { margin-bottom: 18px; font-size: 12px; }
summary { cursor: pointer; color: var(--text-secondary); }
.range-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; margin-top: 14px; }
fieldset { border: 0; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; min-width: 0; }
legend { padding-bottom: 8px; font-weight: 600; }
label { display: grid; gap: 6px; color: var(--text-secondary); }
input { width: 100%; min-width: 0; padding: 9px 10px; background: var(--surface); border: 1px solid var(--border-strong); border-radius: 6px; color: var(--text); font: inherit; }
.test-note { margin-top: 12px; }
.test-result { margin-top: 20px; padding-top: 16px; border-top: 1px solid var(--border); }
.result-heading { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 8px; }
.result-heading h3 { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.result-heading button { font-size: 13px; padding: 10px 12px; }
.result-loading { color: var(--text-secondary); font-size: 12px; margin-top: 10px; }
.result-error { color: #9d302a; font-size: 13px; margin-top: 10px; }
.json-preview { margin-top: 10px; font-size: 12px; }
pre { max-height: 360px; overflow: auto; margin-top: 10px; padding: 16px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); color: var(--text); font: 12px/1.65 Consolas, monospace; tab-size: 2; }
.spin { animation: spin 1s linear infinite; }
@media (max-width: 700px) { .range-grid { grid-template-columns: 1fr; } }
</style>
