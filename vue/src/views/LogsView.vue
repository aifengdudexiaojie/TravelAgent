<script setup lang="ts">
/**
 * 后端日志（开发调试）
 *
 * 后端通过 run_backend.py 启动时会把 stdout/stderr（含 uvicorn 访问日志、项目
 * logger、以及代码里所有 print 的进度信息）tee 到 logs/backend.log；
 * 本页通过 /api/dev/logs/stream 实时跟随该文件，所以不用开终端也能看到后端输出。
 */
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import { devApi, streamLogs } from '@/lib/api'

const lines = ref<string[]>([])
const paused = ref(false)
const autoScroll = ref(true)
const filter = ref('')
const logFile = ref('')
const connected = ref(false)
const error = ref('')

const MAX_LINES = 2000

let closeStream: (() => void) | null = null
let scrollTimer: number | null = null

const shownLines = computed(() =>
  filter.value ? lines.value.filter((l) => l.includes(filter.value)) : lines.value,
)

function pushLine(text: string) {
  if (paused.value) return
  lines.value.push(text)
  if (lines.value.length > MAX_LINES) {
    lines.value.splice(0, lines.value.length - MAX_LINES)
  }
  scheduleScroll()
}

function scheduleScroll() {
  if (!autoScroll.value || scrollTimer !== null) return
  scrollTimer = window.setTimeout(async () => {
    scrollTimer = null
    await nextTick()
    const el = document.getElementById('log-scroll')
    if (el) el.scrollTop = el.scrollHeight
  }, 60)
}

function connect() {
  closeStream?.()
  connected.value = true
  error.value = ''
  closeStream = streamLogs(pushLine, { tail: 300 })
}

async function reload() {
  try {
    const resp = await devApi.logs(500)
    logFile.value = resp.data?.file || ''
    lines.value = resp.data?.lines || []
    if (!resp.data?.exists) {
      error.value = `日志文件还不存在：${logFile.value}（请用 python run_backend.py 启动后端）`
    }
    await nextTick()
    const el = document.getElementById('log-scroll')
    if (el) el.scrollTop = el.scrollHeight
  } catch (err: any) {
    error.value = err?.response?.data?.detail || err?.message || '读取日志失败'
  }
}

function clearView() {
  lines.value = []
}

function lineClass(line: string): string {
  if (line.includes(' ERROR ') || line.startsWith('ERROR') || line.includes('Traceback')) return 'text-red-400'
  if (line.includes(' WARNING ') || line.includes('⚠')) return 'text-amber-300'
  if (line.includes('SSE') || line.includes('分析任务')) return 'text-emerald-300'
  return 'text-gray-300'
}

onMounted(async () => {
  await reload()
  connect()
})

onBeforeUnmount(() => {
  closeStream?.()
  closeStream = null
  if (scrollTimer !== null) window.clearTimeout(scrollTimer)
})
</script>

<template>
  <div class="space-y-3">
    <div class="flex items-center justify-between flex-wrap gap-2">
      <h2 class="text-lg font-bold text-gray-800">🖥️ 后端日志</h2>
      <div class="flex items-center gap-2 text-xs">
        <span class="flex items-center gap-1">
          <span :class="connected ? 'bg-green-500' : 'bg-gray-400'" class="inline-block w-2 h-2 rounded-full"></span>
          {{ connected ? '实时跟随中' : '未连接' }}
        </span>
        <span class="text-gray-400">共 {{ lines.length }} 行（上限 {{ MAX_LINES }}）</span>
      </div>
    </div>

    <div class="flex flex-wrap items-center gap-2">
      <input
        v-model="filter"
        placeholder="过滤关键字，如 task_id / SSE / ERROR"
        class="flex-1 min-w-[200px] px-3 py-2 text-xs border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-400"
      />
      <label class="flex items-center gap-1 text-xs text-gray-600">
        <input type="checkbox" v-model="autoScroll" class="w-3.5 h-3.5 accent-blue-500" />自动滚动
      </label>
      <button
        @click="paused = !paused"
        class="px-3 py-2 text-xs rounded-lg border transition"
        :class="paused ? 'border-amber-300 bg-amber-50 text-amber-700' : 'border-gray-200 text-gray-600 hover:bg-gray-50'"
      >
        {{ paused ? '▶ 继续接收' : '⏸ 暂停' }}
      </button>
      <button @click="reload" class="px-3 py-2 text-xs rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50">
        🔄 重新加载
      </button>
      <button @click="clearView" class="px-3 py-2 text-xs rounded-lg border border-gray-200 text-gray-600 hover:bg-gray-50">
        🧹 清屏
      </button>
    </div>

    <p class="text-xs text-gray-400 break-all">
      日志文件：{{ logFile || '（未获取到）' }}
    </p>

    <div v-if="error" class="p-3 bg-amber-50 border border-amber-200 rounded-lg text-amber-700 text-xs">
      {{ error }}
    </div>

    <div
      id="log-scroll"
      class="bg-gray-900 rounded-xl p-3 h-[calc(100vh-300px)] overflow-auto font-mono text-[11px] leading-relaxed"
    >
      <div v-if="shownLines.length === 0" class="text-gray-500">
        {{ lines.length === 0 ? '暂无日志输出…' : '没有匹配的行' }}
      </div>
      <div v-for="(line, idx) in shownLines" :key="idx" :class="lineClass(line)" class="whitespace-pre-wrap break-all">
        {{ line }}
      </div>
    </div>

    <p class="text-xs text-gray-400">
      提示：也可以用 <code class="bg-gray-100 px-1 rounded">python run_backend.py</code> 在终端启动后端，
      输出同样会写到上面的日志文件（终端 + 文件双写）。
    </p>
  </div>
</template>
