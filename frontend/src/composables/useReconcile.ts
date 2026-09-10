import { ref } from 'vue'
import { demoReconcile } from '../lib/demoReconcile'
import { timestamp } from '../lib/download'

export type Phase = 'idle' | 'processing' | 'success' | 'error'

export interface ReconcileStats {
  matched: number
  mismatched: number
  onlyA: number
  onlyB: number
}

export interface ReconcileResult {
  blob: Blob
  filename: string
  stats: ReconcileStats | null
}

const STAGES = ['上传账单', '解析数据', '智能比对', '生成报表'] as const

class BackendUnavailable extends Error {
  constructor() {
    super('backend unavailable')
  }
}

async function requestBackend(fileA: File, fileB: File): Promise<ReconcileResult> {
  const fd = new FormData()
  fd.append('file_a', fileA)
  fd.append('file_b', fileB)

  let res: Response
  try {
    res = await fetch('/api/reconcile', { method: 'POST', body: fd })
  } catch {
    throw new BackendUnavailable()
  }
  if (res.status === 404) throw new BackendUnavailable()
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    throw new Error(text.trim() || `服务器返回错误（HTTP ${res.status}）`)
  }

  const blob = await res.blob()
  const dispo = res.headers.get('Content-Disposition') ?? ''
  const m = /filename\*?=(?:UTF-8''|")?([^";]+)/i.exec(dispo)
  const filename = m?.[1] ? decodeURIComponent(m[1].replace(/"/g, '')) : `对账结果_${timestamp()}.xlsx`
  return { blob, filename, stats: null }
}

export function useReconcile() {
  const phase = ref<Phase>('idle')
  const stageIndex = ref(0)
  const stages = [...STAGES]
  const result = ref<ReconcileResult | null>(null)
  const errorMsg = ref('')
  const isDemo = ref(false)

  let timers: number[] = []

  function clearTimers() {
    timers.forEach((t) => clearTimeout(t))
    timers = []
  }

  /** 处理期间按节奏推进阶段指示器 */
  function advanceStages(totalMs: number) {
    clearTimers()
    stageIndex.value = 0
    const step = totalMs / STAGES.length
    STAGES.forEach((_, i) => {
      if (i === 0) return
      timers.push(window.setTimeout(() => (stageIndex.value = i), i * step))
    })
  }

  async function start(fileA: File, fileB: File) {
    phase.value = 'processing'
    errorMsg.value = ''
    isDemo.value = false
    result.value = null
    advanceStages(5200)

    const startedAt = performance.now()
    try {
      result.value = await requestBackend(fileA, fileB)
    } catch (err) {
      if (err instanceof BackendUnavailable) {
        // 后端未就绪时回退到本地演示模式
        isDemo.value = true
        try {
          const demo = await demoReconcile(fileA, fileB)
          result.value = { blob: demo.blob, filename: demo.filename, stats: demo.stats }
        } catch (demoErr) {
          phase.value = 'error'
          errorMsg.value = demoErr instanceof Error ? demoErr.message : '文件解析失败，请重试'
          clearTimers()
          return
        }
      } else {
        phase.value = 'error'
        errorMsg.value = err instanceof Error ? err.message : '对账失败，请稍后重试'
        clearTimers()
        return
      }
    }

    // 保证进度动画至少播放一个完整节奏，体验更顺滑
    const elapsed = performance.now() - startedAt
    const minDuration = 2600
    if (elapsed < minDuration) {
      await new Promise((r) => setTimeout(r, minDuration - elapsed))
    }

    clearTimers()
    stageIndex.value = STAGES.length
    phase.value = 'success'
  }

  function reset() {
    clearTimers()
    phase.value = 'idle'
    stageIndex.value = 0
    result.value = null
    errorMsg.value = ''
    isDemo.value = false
  }

  return { phase, stageIndex, stages, result, errorMsg, isDemo, start, reset }
}
