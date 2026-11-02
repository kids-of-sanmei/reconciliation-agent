<script setup lang="ts">
import { Download, FileSpreadsheet } from 'lucide-vue-next'
import { taskUrl, type TaskView } from '../lib/preparation'

defineProps<{ task: TaskView }>()
const labels: Record<string, string> = {
  'c_baseline.xlsx': 'C 初始底稿', 'audit.json': '底稿清理审计', 'prepared.json': '数据准备包',
  'a_records.json': 'A 标准化明细', 'b_records.json': 'B 标准化明细', 'b_index.json': 'B 查询索引',
}
</script>

<template>
  <section class="results" aria-labelledby="results-title">
    <div class="result-heading">
      <div>
        <h3 id="results-title">本次生成的文件</h3>
        <p>数据准备已完成，后续逐行匹配与最终核查尚未执行。</p>
      </div>
      <FileSpreadsheet :size="24" aria-hidden="true" />
    </div>
    <ul>
      <li v-for="name in task.artifact_names" :key="name">
        <a :href="`${taskUrl(task.task_id)}/artifacts/${encodeURIComponent(name)}`" :download="name">
          <span>{{ labels[name] ?? name }}<small>{{ name }}</small></span>
          <Download :size="16" aria-hidden="true" />
        </a>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.results { margin-top: 32px; }
.result-heading { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 16px; }
h3 { font-size: 17px; font-weight: 600; }
p { font-size: 13px; color: var(--text-secondary); margin-top: 4px; }
ul { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); list-style: none; gap: 0 28px; }
a { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 0; border-bottom: 1px solid var(--border); color: var(--accent); font-size: 14px; text-decoration: none; }
a:hover { color: var(--primary-hover); }
small { display: block; font-size: 11px; color: var(--text-tertiary); margin-top: 2px; }
@media (max-width: 560px) { ul { grid-template-columns: 1fr; } }
</style>
