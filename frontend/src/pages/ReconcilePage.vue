<script setup lang="ts">
import { computed, ref } from 'vue'
import { ArrowLeftRight, CircleAlert, Loader2 } from 'lucide-vue-next'
import UploadCard from '../components/UploadCard.vue'
import ProgressFlow from '../components/ProgressFlow.vue'
import ResultPanel from '../components/ResultPanel.vue'
import { useReconcile } from '../composables/useReconcile'

const fileA = ref<File | null>(null)
const fileB = ref<File | null>(null)

const { phase, stageIndex, stages, result, errorMsg, isDemo, start, reset } = useReconcile()

const canStart = computed(() => !!fileA.value && !!fileB.value && phase.value !== 'processing')

function onStart() {
  if (fileA.value && fileB.value) start(fileA.value, fileB.value)
}

function onReset() {
  fileA.value = null
  fileB.value = null
  reset()
}
</script>

<template>
  <main class="page">
    <div class="page-inner">
      <!-- 任务入口标题区 -->
      <header class="masthead">
        <span class="logo-mark" aria-hidden="true">
          <ArrowLeftRight :size="22" :stroke-width="2" />
        </span>
        <h1 class="title">清账</h1>
        <p class="subtitle">上传两份 Excel 账单，一键生成对账报表</p>
      </header>

      <!-- 上传区 -->
      <div class="upload-grid">
        <UploadCard
          v-model="fileA"
          label="平台账单"
          hint="点击选择，或将文件拖到这里"
          :disabled="phase === 'processing'"
        />
        <UploadCard
          v-model="fileB"
          label="内部账单"
          hint="点击选择，或将文件拖到这里"
          :disabled="phase === 'processing'"
        />
      </div>

      <!-- 操作区 -->
      <div class="action-row">
        <button class="btn btn-primary btn-lg" type="button" :disabled="!canStart" @click="onStart">
          <Loader2 v-if="phase === 'processing'" :size="17" class="spin" aria-hidden="true" />
          {{ phase === 'processing' ? '正在对账…' : '开始对账' }}
        </button>
        <button
          v-if="(fileA || fileB) && phase !== 'processing'"
          class="btn btn-ghost"
          type="button"
          @click="onReset"
        >
          清空重选
        </button>
      </div>
      <p v-if="!canStart && phase === 'idle'" class="action-hint">请先上传两份账单文件</p>

      <!-- 进度 / 结果 / 错误 -->
      <Transition name="fade" mode="out-in">
        <div v-if="phase === 'processing'" key="progress" class="status-area">
          <ProgressFlow :stages="stages" :stage-index="stageIndex" :phase="phase" />
        </div>

        <div v-else-if="phase === 'success' && result" key="success" class="status-area">
          <ResultPanel :result="result" :is-demo="isDemo" @reset="onReset" />
        </div>

        <div v-else-if="phase === 'error'" key="error" class="status-area">
          <div class="material-panel error-panel" role="alert">
            <span class="badge-error" aria-hidden="true">
              <CircleAlert :size="26" :stroke-width="2" />
            </span>
            <h3 class="error-title">对账没有完成</h3>
            <p class="error-desc">{{ errorMsg }}</p>
            <div class="error-actions">
              <button class="btn btn-primary" type="button" @click="onStart">重试一次</button>
              <button class="btn btn-ghost" type="button" @click="onReset">重新选择文件</button>
            </div>
          </div>
        </div>
      </Transition>

      <p class="footnote">支持 .xlsx / .xls / .csv 格式 · 单文件最大 20 MB</p>
    </div>
  </main>
</template>

<style scoped>
.page {
  min-height: 100dvh;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 96px 24px 64px;
}

.page-inner {
  width: 100%;
  max-width: 880px;
  text-align: center;
}

/* 标题区 —— 大留白建立高级感 */
.masthead {
  margin-bottom: 88px;
}

.logo-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  border-radius: 12px;
  background: var(--primary);
  color: #fff;
  margin-bottom: 24px;
  box-shadow: var(--shadow-card);
}

.title {
  font-size: 40px;
  font-weight: 700;
  letter-spacing: 0;
  margin-bottom: 12px;
}

.subtitle {
  font-size: 17px;
  line-height: 1.6;
  color: var(--text-secondary);
}

/* 上传区 —— 用间距而非分割线分隔 */
.upload-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 32px;
  margin-bottom: 64px;
}

/* 操作区 */
.action-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
}

.btn-lg {
  font-size: 16px;
  padding: 15px 40px;
}

.spin {
  animation: spin 0.8s linear infinite;
}

.action-hint {
  margin-top: 16px;
  font-size: 13px;
  color: var(--text-tertiary);
}

.status-area {
  margin-top: 56px;
  text-align: left;
}

/* 错误面板 */
.error-panel {
  padding: 44px 40px;
  text-align: center;
}

.badge-error {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  border-radius: 50%;
  background: rgb(255 95 87 / 10%);
  color: var(--error);
  margin-bottom: 18px;
}

.error-title {
  font-size: 22px;
  font-weight: 600;
  letter-spacing: 0;
  margin-bottom: 8px;
}

.error-desc {
  font-size: 14px;
  color: var(--text-secondary);
  max-width: 460px;
  margin: 0 auto 26px;
  line-height: 1.6;
  word-break: break-all;
}

.error-actions {
  display: flex;
  justify-content: center;
  gap: 8px;
  flex-wrap: wrap;
}

.footnote {
  margin-top: 80px;
  font-size: 12px;
  color: var(--text-tertiary);
}

/* 断点设置明确字号，不做连续缩放 */
@media (max-width: 720px) {
  .page {
    align-items: flex-start;
    padding-top: 64px;
  }

  .masthead {
    margin-bottom: 56px;
  }

  .title {
    font-size: 32px;
  }

  .upload-grid {
    grid-template-columns: 1fr;
    gap: 20px;
    margin-bottom: 48px;
  }

  .footnote {
    margin-top: 56px;
  }
}
</style>
