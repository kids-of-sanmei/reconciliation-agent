import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { isTerminal, requestTask, taskUrl, type ConfirmationInput, type TaskView } from '../lib/preparation'

const STORAGE_KEY = 'reconciliation.current-task'

export function useReconcile() {
  const task = ref<TaskView | null>(null)
  const errorMsg = ref('')
  const submitting = ref(false)
  const syncing = ref(false)
  const lastSyncedAt = ref('')
  const processing = computed(() => submitting.value || (!!task.value && !isTerminal(task.value)))
  let timer: ReturnType<typeof setTimeout> | undefined
  let generation = 0

  function stopPolling() {
    generation += 1
    clearTimeout(timer)
    syncing.value = false
  }

  function remember(view: TaskView) {
    task.value = view
    lastSyncedAt.value = new Date().toISOString()
    try { localStorage.setItem(STORAGE_KEY, view.task_id) } catch { /* 浏览器可能禁用存储。 */ }
    const url = new URL(window.location.href)
    url.searchParams.set('task', view.task_id)
    window.history.replaceState(null, '', url)
  }

  async function poll(id: string, current: number) {
    if (current !== generation) return
    syncing.value = true
    try {
      const view = await requestTask(taskUrl(id))
      if (current !== generation) return
      remember(view)
      errorMsg.value = ''
      if (submitting.value || !isTerminal(view)) {
        timer = setTimeout(() => void poll(id, current), 800)
      }
    } catch (error) {
      if (current !== generation) return
      errorMsg.value = error instanceof Error ? error.message : '更新任务状态失败'
      timer = setTimeout(() => void poll(id, current), 3000)
    } finally {
      if (current === generation) syncing.value = false
    }
  }

  function watchTask() {
    stopPolling()
    if (task.value) void poll(task.value.task_id, generation)
  }

  async function loadTask(id: string) {
    if (submitting.value) return
    stopPolling()
    const current = generation
    submitting.value = true
    errorMsg.value = ''
    try {
      const view = await requestTask(taskUrl(id.trim()))
      if (current === generation) remember(view)
    } catch (error) {
      if (current === generation) errorMsg.value = error instanceof Error ? error.message : '读取任务失败'
    } finally {
      if (current === generation) {
        submitting.value = false
        if (task.value && !isTerminal(task.value)) watchTask()
      }
    }
  }

  async function start(fileA: File, fileB: File, period: string) {
    if (processing.value || task.value) return
    stopPolling()
    const current = generation
    submitting.value = true
    errorMsg.value = ''
    const data = new FormData()
    data.append('file_a', fileA)
    data.append('file_b', fileB)
    data.append('options', JSON.stringify({ reconciliation_period: period || null }))
    try {
      const view = await requestTask('/api/preparation/tasks', { method: 'POST', body: data })
      if (current === generation) remember(view)
    } catch (error) {
      if (current === generation) errorMsg.value = error instanceof Error ? error.message : '上传失败'
    } finally {
      if (current === generation) {
        submitting.value = false
        if (task.value) watchTask()
      }
    }
  }

  async function act(action: 'run' | 'confirm', confirmation?: ConfirmationInput) {
    if (!task.value || submitting.value) return
    const id = task.value.task_id
    stopPolling()
    const current = generation
    submitting.value = true
    errorMsg.value = ''
    timer = setTimeout(() => void poll(id, current), 300)
    try {
      const view = await requestTask(`${taskUrl(id)}/${action}`, {
        method: 'POST',
        ...(confirmation ? {
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(confirmation),
        } : {}),
      })
      if (current === generation) remember(view)
    } catch (error) {
      if (current === generation) {
        stopPolling()
        errorMsg.value = error instanceof Error ? error.message : '操作失败'
        submitting.value = false
        if (task.value && !isTerminal(task.value)) {
          const next = generation
          timer = setTimeout(() => void poll(id, next), 3000)
        }
      }
    } finally {
      if (current === generation) {
        submitting.value = false
        // 使请求期间发出的查询失效，避免旧状态覆盖最终结果。
        stopPolling()
        if (task.value && !isTerminal(task.value)) watchTask()
      }
    }
  }

  function reset() {
    stopPolling()
    task.value = null
    submitting.value = false
    errorMsg.value = ''
    try { localStorage.removeItem(STORAGE_KEY) } catch { /* 保持页面可用。 */ }
    const url = new URL(window.location.href)
    url.searchParams.delete('task')
    window.history.replaceState(null, '', url)
  }

  onMounted(() => {
    let id = new URLSearchParams(window.location.search).get('task')
    try { id ||= localStorage.getItem(STORAGE_KEY) } catch { /* 可使用网址恢复。 */ }
    if (id) void loadTask(id)
  })
  onBeforeUnmount(stopPolling)

  return {
    task, errorMsg, submitting, syncing, processing, lastSyncedAt,
    start, loadTask, refresh: watchTask, retry: () => act('run'),
    confirm: (input: ConfirmationInput) => act('confirm', input), reset,
  }
}
