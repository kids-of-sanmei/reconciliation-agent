<script setup lang="ts">
import { computed, ref } from 'vue'
import { CircleAlert, FileSpreadsheet, UploadCloud, X } from 'lucide-vue-next'

const props = defineProps<{
  modelValue: File | null
  label: string
  hint: string
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [file: File | null]
}>()

const ACCEPT = ['.xlsx', '.xls', '.csv']
const MAX_SIZE = 20 * 1024 * 1024

const inputRef = ref<HTMLInputElement | null>(null)
const dragover = ref(false)
const errorMsg = ref('')

const zoneState = computed(() => {
  if (props.modelValue) return 'filled'
  if (dragover.value) return 'dragover'
  return 'empty'
})

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function validate(file: File): string {
  const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase()
  if (!ACCEPT.includes(ext)) return `不支持 ${ext || '该'} 格式，请上传 ${ACCEPT.join(' / ')} 文件`
  if (file.size > MAX_SIZE) return `文件超过 20 MB（当前 ${formatSize(file.size)}），请压缩后重试`
  return ''
}

function pick(file: File | undefined | null) {
  if (!file) return
  const err = validate(file)
  errorMsg.value = err
  emit('update:modelValue', err ? null : file)
}

function openPicker() {
  if (!props.disabled) inputRef.value?.click()
}

function onInputChange(e: Event) {
  pick((e.target as HTMLInputElement).files?.[0])
  ;(e.target as HTMLInputElement).value = ''
}

function onDrop(e: DragEvent) {
  dragover.value = false
  if (props.disabled) return
  pick(e.dataTransfer?.files?.[0])
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    openPicker()
  }
}

function clear(e: MouseEvent) {
  e.stopPropagation()
  errorMsg.value = ''
  emit('update:modelValue', null)
}
</script>

<template>
  <div class="upload-card">
    <p class="card-label">{{ label }}</p>

    <div
      class="zone"
      :class="[`is-${zoneState}`, { 'is-disabled': disabled, 'has-error': !!errorMsg }]"
      role="button"
      :tabindex="disabled ? -1 : 0"
      :aria-label="modelValue ? `${label}：已选择 ${modelValue.name}，激活可重新选择` : `${label}：${hint}`"
      @click="openPicker"
      @keydown="onKeydown"
      @dragenter.prevent="!disabled && (dragover = true)"
      @dragover.prevent="!disabled && (dragover = true)"
      @dragleave.prevent="dragover = false"
      @drop.prevent="onDrop"
    >
      <!-- 空态 -->
      <div v-if="!modelValue" class="zone-empty">
        <span class="icon-wrap" aria-hidden="true">
          <UploadCloud :size="26" :stroke-width="1.8" />
        </span>
        <span class="zone-hint">{{ hint }}</span>
        <span class="zone-formats">.xlsx / .xls / .csv</span>
      </div>

      <!-- 已选择 -->
      <div v-else class="zone-file">
        <span class="file-icon" aria-hidden="true">
          <FileSpreadsheet :size="20" :stroke-width="1.8" />
        </span>
        <span class="file-meta">
          <span class="file-name">{{ modelValue.name }}</span>
          <span class="file-size">{{ formatSize(modelValue.size) }}</span>
        </span>
        <button
          v-if="!disabled"
          class="file-clear"
          type="button"
          :aria-label="`移除${label}文件`"
          @click="clear"
        >
          <X :size="15" :stroke-width="2.2" />
        </button>
      </div>
    </div>

    <input
      ref="inputRef"
      type="file"
      :accept="ACCEPT.join(',')"
      hidden
      :disabled="disabled"
      @change="onInputChange"
    />

    <!-- 错误信息固定区域，避免布局跳动 -->
    <p class="card-error" :class="{ visible: !!errorMsg }" role="alert">
      <CircleAlert v-if="errorMsg" :size="13" :stroke-width="2.2" aria-hidden="true" />
      {{ errorMsg || ' ' }}
    </p>
  </div>
</template>

<style scoped>
.upload-card {
  text-align: left;
}

.card-label {
  font-size: 14px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 10px;
}

/* 拖放区：以虚线边界表达，不套卡片 */
.zone {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 148px;
  padding: 28px 22px;
  border: 1.5px dashed var(--border-strong);
  border-radius: var(--radius-panel);
  background: rgb(255 255 255 / 55%);
  cursor: pointer;
  transition:
    border-color var(--duration-fast) ease,
    background-color var(--duration-fast) ease,
    transform var(--duration-fast) ease;
}

.zone:hover:not(.is-disabled) {
  border-color: var(--accent);
}

.zone.is-dragover {
  border-color: var(--accent);
  border-style: solid;
  background: rgb(0 122 255 / 8%);
}

.zone.is-filled {
  border-style: solid;
  border-color: var(--border);
  background: rgb(255 255 255 / 72%);
  cursor: default;
}

.zone.is-disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.zone.has-error {
  border-color: rgb(255 95 87 / 55%);
}

/* 空态 */
.zone-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}

.icon-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  border-radius: 50%;
  background: var(--surface-alt);
  color: var(--text-tertiary);
  margin-bottom: 4px;
  transition:
    color var(--duration-fast) ease,
    background-color var(--duration-fast) ease;
}

.zone.is-dragover .icon-wrap {
  color: var(--accent);
  background: var(--accent-soft);
}

.zone-hint {
  font-size: 14px;
  color: var(--text-secondary);
}

.zone-formats {
  font-size: 12px;
  color: var(--text-tertiary);
}

/* 已选择 */
.zone-file {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
}

.file-icon {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 40px;
  border-radius: var(--radius-control);
  background: var(--accent-soft);
  color: var(--accent);
}

.file-meta {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.file-name {
  font-size: 14px;
  font-weight: 500;
  color: var(--text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.file-size {
  font-size: 12px;
  color: var(--text-tertiary);
}

.file-clear {
  flex: none;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: none;
  border-radius: 50%;
  background: var(--surface-alt);
  color: var(--text-tertiary);
  cursor: pointer;
  transition:
    background-color var(--duration-fast) ease,
    color var(--duration-fast) ease;
}

.file-clear:hover {
  background: rgb(255 95 87 / 12%);
  color: var(--error);
}

/* 错误固定区域 */
.card-error {
  display: flex;
  align-items: center;
  gap: 5px;
  min-height: 22px;
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--error);
  opacity: 0;
  transition: opacity var(--duration-fast) ease;
}

.card-error.visible {
  opacity: 1;
}
</style>
