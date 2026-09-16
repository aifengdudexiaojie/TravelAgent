<script setup lang="ts">
/**
 * 小红书登录状态条 + 扫码登录弹窗（放在「生成旅游攻略」标题旁边）
 *
 * 登录：点「登录小红书」→ 后端调 MCP 的 get_login_qrcode → 网页弹二维码
 *      → 用户用小红书 App 扫码 → 前端每 2.5 秒查状态 → 变绿、弹窗自动关闭。
 *
 * 关于「实例正在启动」：首次运行 MCP 会下载无头浏览器（约 150MB），启动要一会儿。
 * 所以后端把启动改成**后台进行**、接口立刻返回 pending，前端在这里持续轮询直到出图 ——
 * 不会再出现"卡很久最后显示不可用"（那是之前阻塞式等待把请求拖超时导致的）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { apiErrorMessage, xhsApi } from '@/lib/api'

const emit = defineEmits<{
  'update:loggedIn': [value: boolean]
}>()

const status = ref<any>(null)
const fetchingQr = ref(false)
const importing = ref(false)
const clearing = ref(false)
/** 状态条级别的错误（取状态失败等） */
const error = ref('')
const notice = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

/** 二维码弹窗 */
const showQr = ref(false)
const qrImage = ref('')
const qrExpiresAt = ref('')
const qrPending = ref(false)          // 实例还在启动
const qrPendingMsg = ref('')
const qrWaitSec = ref(0)
const qrError = ref('')               // 取码硬失败（例如缺可执行文件）
const qrLogTail = ref('')
const showLogTail = ref(false)
const now = ref(Date.now())

let timer: number | null = null
let countdown: number | null = null
let pollFast = 0
let slowRetry = 0

const loggedIn = computed(() => !!status.value?.logged_in)
const running = computed(() => !!status.value?.mcp_running)
const exeOk = computed(() => status.value?.exe_available !== false)
const platform = computed(() => status.value?.platform || '')
const isServerLinux = computed(() => platform.value && platform.value !== 'win32')

const qrRemain = computed(() => {
  if (!qrExpiresAt.value) return null
  return Math.max(0, Math.round((new Date(qrExpiresAt.value).getTime() - now.value) / 1000))
})
const qrExpired = computed(() => qrRemain.value !== null && qrRemain.value <= 0)

const dotClass = computed(() => {
  if (error.value || !exeOk.value) return 'bg-red-500'
  if (loggedIn.value) return 'bg-green-500'
  return running.value ? 'bg-amber-400' : 'bg-gray-300'
})

const statusText = computed(() => {
  if (error.value) return error.value
  if (loggedIn.value) return status.value?.message || '已登录'
  return status.value?.message || (running.value ? '未登录：点右侧按钮扫码' : '小红书服务未启动')
})

const hint = computed(() => {
  if (exeOk.value) return ''
  return isServerLinux.value
    ? '服务器上缺少 Linux 版小红书服务：按 docs/xhs-multi-user.md 第五节放好 Linux 构建'
    : '缺少小红书服务可执行文件：请查看 xiaohongshumcp/ 目录'
})

async function refresh() {
  try {
    const resp = await xhsApi.status()
    status.value = resp.data
    error.value = ''
    emit('update:loggedIn', !!resp.data?.logged_in)
  } catch (err: any) {
    error.value = apiErrorMessage(err, '无法获取小红书状态')
    emit('update:loggedIn', false)
  }
}

function resetQrState() {
  qrImage.value = ''
  qrExpiresAt.value = ''
  qrPending.value = false
  qrPendingMsg.value = ''
  qrWaitSec.value = 0
  qrError.value = ''
  qrLogTail.value = ''
  showLogTail.value = false
  slowRetry = 0
}

async function openQr() {
  showQr.value = true
  notice.value = ''
  resetQrState()
  if (loggedIn.value) {
    if (!confirm('当前已登录。重新扫码会替换为新的账号，确定继续吗？')) {
      showQr.value = false
      return
    }
    await handleClear(false)
  }
  await fetchQr()
}

/** 取二维码：ok=出图；pending=实例启动中（继续轮询）；否则是硬失败 */
async function fetchQr() {
  if (fetchingQr.value) return
  fetchingQr.value = true
  try {
    const resp = await xhsApi.qrcode()
    const data = resp.data || {}
    if (data.ok) {
      qrImage.value = `data:${data.mime || 'image/png'};base64,${data.image_base64}`
      qrExpiresAt.value = data.expires_at || ''
      qrPending.value = false
      qrError.value = ''
      qrLogTail.value = ''
      pollFast = 120                       // 接下来 ~5 分钟每 2.5s 查一次登录状态
    } else if (data.pending) {
      qrPending.value = true
      qrPendingMsg.value = data.message || '小红书实例正在启动…'
      qrWaitSec.value = data.waited || 0
      qrError.value = ''
    } else {
      qrPending.value = false
      qrError.value = data.message || '未取到二维码'
      qrLogTail.value = data.log_tail || ''
    }
  } catch (err: any) {
    qrPending.value = false
    qrError.value = apiErrorMessage(err, '获取二维码失败')
  } finally {
    fetchingQr.value = false
  }
}

async function handleClear(reopen = true) {
  clearing.value = true
  qrError.value = ''
  notice.value = ''
  try {
    const resp = await xhsApi.clear()
    notice.value = resp.data?.message || '已退出登录'
    await refresh()
    if (reopen) {
      resetQrState()
      await fetchQr()
    }
  } catch (err: any) {
    qrError.value = apiErrorMessage(err, '退出登录失败')
  } finally {
    clearing.value = false
  }
}

async function handleImportCookies(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  importing.value = true
  qrError.value = ''
  notice.value = ''
  try {
    const text = await file.text()
    const resp = await xhsApi.importCookies(text)
    if (resp.data?.ok) {
      notice.value = resp.data.message || 'cookies.json 已导入'
      await refresh()
      pollFast = 8
      if (loggedIn.value) showQr.value = false
    } else {
      qrError.value = resp.data?.message || '导入失败'
    }
  } catch (err: any) {
    qrError.value = apiErrorMessage(err, '导入 cookies.json 失败')
  } finally {
    importing.value = false
    input.value = ''
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

function closeQr() {
  showQr.value = false
}

onMounted(() => {
  refresh()
  countdown = window.setInterval(() => { now.value = Date.now() }, 1000)
  timer = window.setInterval(async () => {
    if (pollFast > 0) pollFast -= 1
    if (loggedIn.value && showQr.value) {
      showQr.value = false
      notice.value = `已登录${status.value?.username ? '：' + status.value.username : ''}`
      return
    }
    if (!loggedIn.value) await refresh()

    if (!showQr.value) return
    // 启动中 → 每次轮询都重试取码；硬失败 → 放慢到每 ~7.5 秒重试一次（避免刷屏）
    if (qrPending.value || !qrImage.value) {
      if (!qrError.value || slowRetry++ % 3 === 0) await fetchQr()
    } else if (qrExpired.value) {
      await fetchQr()                     // 二维码过期自动换新
    }
  }, 2500)
})

onBeforeUnmount(() => {
  if (timer !== null) window.clearInterval(timer)
  if (countdown !== null) window.clearInterval(countdown)
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
          <span
            class="relative inline-flex rounded-full h-2.5 w-2.5"
            :class="status?.starting ? 'bg-amber-400 animate-pulse' : dotClass"
          ></span>
        </span>
        <span class="font-medium shrink-0">小红书</span>
        <span class="truncate">{{ status?.starting ? '实例启动中…' : statusText }}</span>
      </div>

      <button
        @click="openQr"
        :disabled="fetchingQr && !showQr"
        :title="loggedIn ? '换一个账号登录（重新扫码）' : '打开二维码，用小红书 App 扫码登录'"
        class="px-3 py-1.5 text-xs rounded-lg transition disabled:opacity-50 flex items-center gap-1.5 shrink-0"
        :class="loggedIn ? 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50' : 'bg-red-500 text-white hover:bg-red-600'"
      >
        <span v-if="fetchingQr && !showQr" class="inline-block w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin"></span>
        {{ loggedIn ? '🔄 换个账号' : '🔐 登录小红书' }}
      </button>

      <button
        v-if="running && !loggedIn"
        @click="handleStop"
        class="px-3 py-1.5 text-xs rounded-lg bg-white border border-gray-200 text-gray-500 hover:bg-gray-50 transition shrink-0"
        title="停止该用户的小红书实例，释放内存"
      >停止实例</button>
    </div>

    <p v-if="hint" class="text-[11px] text-red-600 max-w-[640px] text-right">{{ hint }}</p>
    <p v-if="notice" class="text-[11px] text-green-700 max-w-[640px] text-right">{{ notice }}</p>
    <p v-if="error && !hint" class="text-[11px] text-red-600 max-w-[640px] text-right whitespace-pre-wrap">{{ error }}</p>

    <!-- 扫码登录弹窗 -->
    <div
      v-if="showQr"
      class="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4"
      @click.self="closeQr"
    >
      <div class="bg-white rounded-2xl w-full max-w-sm p-5 text-left">
        <div class="flex items-start justify-between mb-3">
          <h3 class="text-base font-bold text-gray-800">📱 用小红书 App 扫码登录</h3>
          <button @click="closeQr" class="text-gray-400 hover:text-gray-600 text-xl leading-none">&times;</button>
        </div>

        <div class="flex flex-col items-center">
          <div class="w-56 h-56 border border-gray-100 rounded-xl flex items-center justify-center bg-gray-50 overflow-hidden">
            <!-- ① 出图 -->
            <img v-if="qrImage" :src="qrImage" alt="登录二维码" class="w-full h-full object-contain" />

            <!-- ② 实例启动中（首次要下载浏览器，可能 1~2 分钟） -->
            <div v-else-if="qrPending" class="flex flex-col items-center gap-2 px-4 text-center">
              <span class="inline-block w-8 h-8 border-4 border-blue-400 border-t-transparent rounded-full animate-spin"></span>
              <span class="text-xs text-gray-600">{{ qrPendingMsg }}</span>
              <span v-if="qrWaitSec" class="text-[11px] text-gray-400">已等待 {{ qrWaitSec }} 秒</span>
            </div>

            <!-- ③ 硬失败：显示后端给的真实原因 -->
            <div v-else-if="qrError" class="px-4 text-center">
              <p class="text-xs text-red-600 whitespace-pre-wrap">{{ qrError }}</p>
            </div>

            <span v-else class="text-xs text-gray-400 px-4 text-center">点击下方「刷新二维码」获取</span>
          </div>

          <p v-if="qrImage" class="mt-3 text-xs text-gray-500 text-center">
            打开小红书 App →「我」→ 右上角扫一扫
          </p>
          <p v-if="qrRemain !== null && qrImage" class="mt-1 text-xs" :class="qrExpired ? 'text-amber-600' : 'text-gray-400'">
            {{ qrExpired ? '二维码已过期，正在自动刷新…' : `二维码有效期剩余 ${qrRemain} 秒` }}
          </p>
          <p v-if="loggedIn" class="mt-2 text-xs text-green-600 font-medium">✅ 已登录，正在关闭…</p>
        </div>

        <!-- 启动失败的日志尾部（排查用） -->
        <div v-if="qrLogTail" class="mt-3">
          <button @click="showLogTail = !showLogTail" class="text-[11px] text-blue-600 hover:underline">
            {{ showLogTail ? '收起实例日志' : '查看实例日志（排查用）' }}
          </button>
          <pre v-if="showLogTail" class="mt-1 max-h-40 overflow-auto bg-gray-900 text-gray-200 text-[10px] p-2 rounded-lg whitespace-pre-wrap">{{ qrLogTail }}</pre>
        </div>

        <div class="mt-4 flex flex-wrap items-center gap-2 justify-center">
          <button
            @click="fetchQr"
            :disabled="fetchingQr"
            class="px-3 py-1.5 text-xs rounded-lg bg-blue-600 text-white hover:bg-blue-700 transition disabled:opacity-50"
          >{{ fetchingQr ? '获取中…' : '🔄 刷新二维码' }}</button>
          <button
            @click="fileInput?.click()"
            :disabled="importing"
            class="px-3 py-1.5 text-xs rounded-lg bg-white border border-gray-200 text-gray-600 hover:bg-gray-50 transition disabled:opacity-50"
            title="迁移已有登录态时使用：把本机登录好的 cookies.json 导进来"
          >{{ importing ? '导入中…' : '📄 导入 cookies.json' }}</button>
          <button
            v-if="loggedIn"
            @click="handleClear()"
            :disabled="clearing"
            class="px-3 py-1.5 text-xs rounded-lg bg-white border border-gray-200 text-gray-500 hover:bg-gray-50 transition disabled:opacity-50"
          >{{ clearing ? '处理中…' : '退出登录' }}</button>
          <input ref="fileInput" type="file" accept=".json,application/json" class="hidden" @change="handleImportCookies" />
        </div>

        <p v-if="notice" class="mt-3 text-xs text-green-700 bg-green-50 border border-green-100 rounded-lg px-3 py-2">{{ notice }}</p>
        <p class="mt-3 text-[11px] text-gray-400 leading-relaxed">
          首次登录时服务器要下载无头浏览器（约 150MB），可能等 1~2 分钟，属正常现象。<br />
          登录状态只保存在你自己的账号目录里，与其他用户互不影响；同一个账号不要同时在别处登录网页版。
        </p>
      </div>
    </div>
  </div>
</template>
