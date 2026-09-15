<script setup lang="ts">
/**
 * 小红书登录状态条（放在「生成旅游攻略」标题旁边）
 *
 * 行为：
 *   · 挂载后每 5 秒轮询一次 /api/xhs/status，绿灯=已登录，灰灯=未登录
 *   · 点「登录小红书」→ 后端拉起 xiaohongshu-login-windows-amd64.exe（会弹出浏览器扫码），
 *     同时确保该用户自己的 MCP 实例已启动；登录完成后状态自动变绿
 *   · 状态变化通过 `update:loggedIn` 抛给父组件，用于门禁「开始规划」
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { apiErrorMessage, xhsApi } from '@/lib/api'

const emit = defineEmits<{
  'update:loggedIn': [value: boolean]
}>()

const status = ref<any>(null)
const loading = ref(false)
const loggingIn = ref(false)
const error = ref('')
let timer: number | null = null
let pollFast = 0

const loggedIn = computed(() => !!status.value?.logged_in)
const running = computed(() => !!status.value?.mcp_running)
const modeText = computed(() => (status.value?.multi_user ? '多用户模式（一人一实例）' : '共享模式（共用一个账号）'))

const dotClass = computed(() => {
  if (error.value) return 'bg-red-500'
  if (loggedIn.value) return 'bg-green-500'
  return running.value ? 'bg-amber-400' : 'bg-gray-300'
})

const statusText = computed(() => {
  if (error.value) return '状态获取失败'
  if (loggedIn.value) return status.value?.message || '已登录'
  if (running.value) return '未登录：请扫码登录自己的小红书'
  return '小红书服务未启动'
})

async function refresh() {
  try {
    loading.value = true
    const resp = await xhsApi.status()
    status.value = resp.data
    error.value = ''
    emit('update:loggedIn', !!resp.data?.logged_in)
  } catch (err: any) {
    error.value = apiErrorMessage(err, '无法获取小红书状态')
    emit('update:loggedIn', false)
  } finally {
    loading.value = false
  }
}

async function handleLogin() {
  loggingIn.value = true
  error.value = ''
  try {
    const resp = await xhsApi.login()
    status.value = resp.data?.status || status.value
    pollFast = 40                       // 接下来 ~2 分钟每 3 秒刷新一次，等用户扫码完成
  } catch (err: any) {
    error.value = apiErrorMessage(err, '拉起登录程序失败')
  } finally {
    loggingIn.value = false
  }
}

async function handleStop() {
  try {
    await xhsApi.stopMcp()
    await refresh()
  } catch (err: any) {
    error.value = apiErrorMessage(err, '停止实例失败')
  }
}

onMounted(() => {
  refresh()
  timer = window.setInterval(() => {
    if (pollFast > 0) pollFast -= 1
    if (pollFast > 0 || !loggedIn.value) refresh()
  }, pollFast > 0 ? 3000 : 5000)
})

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer)
})
</script>

<template>
  <div class="flex flex-wrap items-center gap-2">
    <!-- 状态灯 -->
    <div
      class="flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs"
      :class="loggedIn
        ? 'bg-green-50 border-green-200 text-green-700'
        : (running ? 'bg-amber-50 border-amber-200 text-amber-700' : 'bg-gray-50 border-gray-200 text-gray-500')"
      :title="`${modeText}｜MCP：${status?.mcp_url || '-'}${status?.port ? '（端口 ' + status.port + '）' : ''}`"
    >
      <span class="relative flex h-2.5 w-2.5">
        <span
          v-if="loggedIn"
          class="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"
        ></span>
        <span class="relative inline-flex rounded-full h-2.5 w-2.5" :class="dotClass"></span>
      </span>
      <span class="font-medium">小红书</span>
      <span>{{ statusText }}</span>
    </div>

    <button
      @click="handleLogin"
      :disabled="loggingIn"
      :title="loggedIn ? '重新登录（会覆盖当前登录态）' : '打开登录窗口扫码登录自己的小红书账号'"
      class="px-3 py-1.5 text-xs rounded-lg transition disabled:opacity-50 flex items-center gap-1.5"
      :class="loggedIn ? 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50' : 'bg-red-500 text-white hover:bg-red-600'"
    >
      <span v-if="loggingIn" class="inline-block w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin"></span>
      {{ loggingIn ? '正在打开登录窗口…' : (loggedIn ? '🔄 重新登录' : '🔐 登录小红书') }}
    </button>

    <button
      v-if="running && !loggedIn"
      @click="handleStop"
      class="px-3 py-1.5 text-xs rounded-lg bg-white border border-gray-200 text-gray-500 hover:bg-gray-50 transition"
      title="停止该用户的小红书实例，释放内存"
    >停止实例</button>
  </div>
</template>
