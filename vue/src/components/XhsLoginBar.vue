<script setup lang="ts">
/**
 * 小红书登录状态条（放在「生成旅游攻略」标题旁边）
 *
 * 两种登录方式，按环境自动适配：
 *   · 桌面环境（本机/有桌面的服务器）：点「登录小红书」弹出扫码窗口
 *   · 无桌面服务器（云端 Linux）：服务器上跑不了 Windows exe，也弹不出浏览器 →
 *     用「导入 cookies.json」把本机登录好的登录态传上去
 *
 * 状态：绿灯=已登录；灰/红灯=未登录（此时父组件会禁用「开始规划」）。
 * 服务器侧的失败原因（缺可执行文件、平台不对、权限不足等）会原样展示出来。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { apiErrorMessage, xhsApi } from '@/lib/api'

const emit = defineEmits<{
  'update:loggedIn': [value: boolean]
}>()

const status = ref<any>(null)
const loading = ref(false)
const loggingIn = ref(false)
const importing = ref(false)
const error = ref('')
const notice = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
let timer: number | null = null
let pollFast = 0

const loggedIn = computed(() => !!status.value?.logged_in)
const running = computed(() => !!status.value?.mcp_running)
const exeOk = computed(() => status.value?.exe_available !== false)
const loginExeOk = computed(() => status.value?.login_exe_available !== false)
const platform = computed(() => status.value?.platform || '')
const isServerLinux = computed(() => platform.value && platform.value !== 'win32')

/** 登录程序不可用（如 Linux 服务器没有 Linux 版登录工具）→ 禁用按钮并引导去用导入 */
const loginDisabled = computed(() => loggingIn.value || !loginExeOk.value)
const loginTitle = computed(() => {
  if (loggedIn.value) return '重新登录（会覆盖当前登录态）'
  if (!loginExeOk.value) {
    return isServerLinux.value
      ? '服务器上没有 Linux 版登录工具，弹不出扫码窗口 → 请用「📄 导入 cookies.json」'
      : '缺少登录程序 → 请用「📄 导入 cookies.json」'
  }
  return '打开登录窗口扫码登录自己的小红书账号（需要桌面环境）'
})

const dotClass = computed(() => {
  if (error.value || !exeOk.value) return 'bg-red-500'
  if (loggedIn.value) return 'bg-green-500'
  return running.value ? 'bg-amber-400' : 'bg-gray-300'
})

/** 展示后端给的原因（含"服务器上没有可用程序"这类关键信息），而不是笼统的"未启动" */
const statusText = computed(() => {
  if (error.value) return error.value
  if (loggedIn.value) return status.value?.message || '已登录'
  return status.value?.message || (running.value ? '未登录：请扫码登录自己的小红书' : '小红书服务未启动')
})

/** 服务器侧不可用时，给出可执行的下一步 */
const hint = computed(() => {
  if (exeOk.value) return ''
  return isServerLinux.value
    ? '服务器上缺少 Linux 版小红书服务：请按 docs/xhs-multi-user.md 第五节安装，或改用「导入 cookies.json」'
    : '缺少小红书服务可执行文件：请看下方错误详情'
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
  notice.value = ''
  try {
    const resp = await xhsApi.login()
    status.value = resp.data?.status || status.value
    // 后端把失败原因放在 mcp.message / login.message 里，直接展示
    const msg = resp.data?.login?.ok ? resp.data?.message : (resp.data?.mcp?.message || resp.data?.message)
    if (resp.data?.login?.ok) {
      notice.value = msg || '已打开登录窗口，请扫码'
      pollFast = 40                      // 接下来 ~2 分钟每 3 秒刷新，等扫码完成
    } else {
      error.value = msg || '无法拉起登录程序'
    }
  } catch (err: any) {
    error.value = apiErrorMessage(err, '拉起登录程序失败')
  } finally {
    loggingIn.value = false
  }
}

async function handleImportCookies(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  importing.value = true
  error.value = ''
  notice.value = ''
  try {
    const text = await file.text()
    const resp = await xhsApi.importCookies(text)
    if (resp.data?.ok) {
      notice.value = resp.data.message || 'cookies.json 已导入'
      await refresh()
      pollFast = 6
    } else {
      error.value = resp.data?.message || '导入失败'
    }
  } catch (err: any) {
    error.value = apiErrorMessage(err, '导入 cookies.json 失败')
  } finally {
    importing.value = false
    input.value = ''                      // 允许重复选同一个文件
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
  <div class="flex flex-col items-end gap-1">
    <div class="flex flex-wrap items-center gap-2">
      <!-- 状态灯 -->
      <div
        class="flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs max-w-[520px]"
        :class="loggedIn
          ? 'bg-green-50 border-green-200 text-green-700'
          : (exeOk ? 'bg-amber-50 border-amber-200 text-amber-700' : 'bg-red-50 border-red-200 text-red-700')"
        :title="status?.message || ''"
      >
        <span class="relative flex h-2.5 w-2.5 shrink-0">
          <span v-if="loggedIn" class="animate-ping absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75"></span>
          <span class="relative inline-flex rounded-full h-2.5 w-2.5" :class="dotClass"></span>
        </span>
        <span class="font-medium shrink-0">小红书</span>
        <span class="truncate">{{ statusText }}</span>
      </div>

      <button
        @click="handleLogin"
        :disabled="loginDisabled"
        :title="loginTitle"
        class="px-3 py-1.5 text-xs rounded-lg transition disabled:opacity-50 flex items-center gap-1.5 shrink-0"
        :class="loginDisabled
          ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
          : (loggedIn ? 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50' : 'bg-red-500 text-white hover:bg-red-600')"
      >
        <span v-if="loggingIn" class="inline-block w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin"></span>
        {{ loggingIn ? '正在打开登录窗口…' : (loggedIn ? '🔄 重新登录' : '🔐 登录小红书') }}
      </button>

      <!-- 无桌面服务器的登录方式 -->
      <button
        @click="fileInput?.click()"
        :disabled="importing"
        title="把在本机登录好的 cookies.json 内容导入到服务器（云端 Linux 上唯一可行的登录方式）"
        class="px-3 py-1.5 text-xs rounded-lg bg-white border border-gray-200 text-gray-600 hover:bg-gray-50 transition disabled:opacity-50 shrink-0"
      >
        {{ importing ? '导入中…' : '📄 导入 cookies.json' }}
      </button>
      <input
        ref="fileInput"
        type="file"
        accept=".json,application/json"
        class="hidden"
        @change="handleImportCookies"
      />

      <button
        v-if="running && !loggedIn"
        @click="handleStop"
        class="px-3 py-1.5 text-xs rounded-lg bg-white border border-gray-200 text-gray-500 hover:bg-gray-50 transition shrink-0"
        title="停止该用户的小红书实例，释放内存"
      >停止实例</button>
    </div>

    <p v-if="hint" class="text-[11px] text-red-600 max-w-[620px] text-right">{{ hint }}</p>
    <p v-if="notice" class="text-[11px] text-green-700 max-w-[620px] text-right">{{ notice }}</p>
    <p v-if="error && !hint" class="text-[11px] text-red-600 max-w-[620px] text-right whitespace-pre-wrap">{{ error }}</p>
  </div>
</template>
