<script setup lang="ts">
/**
 * 小红书登录状态条 + 扫码登录弹窗（放在「生成旅游攻略」标题旁边）
 *
 * 登录有两条路：
 *  ① **网页扫码登录（推荐，默认走这条）**：后端自己开一个浏览器（`/api/xhs/login/start`），
 *     每次探测都返回**当前**二维码 —— 小红书自己的码约 1~2 分钟就换一次，所以
 *     "每次都是新图"才能保证用户扫到的不是废码；出现二次设备安全验证时，
 *     后端会把那一张码也返回（state=verify），前端提示"再扫这张"。
 *     登录成功后后端直接按 MCP 的格式写 cookies.json，状态灯立刻变绿。
 *  ② MCP 的静态二维码（`/api/xhs/qrcode`）：后端没装 playwright 时自动退回，
 *     但它只截一张图、也检测不到二次验证（上游 issue #799），所以只作备用。
 *
 * 无论哪条路：**扫码后在手机上点「确认登录」才算完成**。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { apiErrorMessage, xhsApi } from '@/lib/api'

const emit = defineEmits<{
  'update:loggedIn': [value: boolean]
}>()

const status = ref<any>(null)
const fetchingQr = ref(false)
const refreshing = ref(false)         // 状态请求防重入（后端可能耗时数秒）
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
/** ① 实时扫码登录（服务器自建浏览器）相关状态 */
const liveMode = ref(false)           // 当前是否走"实时二维码"这条路
const liveState = ref('')             // qr / verify / waiting / logged_in / unavailable / error
const liveNote = ref('')              // 后端给的状态说明
const liveAvail = ref<any>(null)      // /login/available 的结果
const qrSeq = ref(0)                  // 第几张码（用来提示用户"这是最新的一张"）
/** 服务器浏览器画面快照（排错用） */
const liveScreen = ref<any>(null)
const screenLoading = ref(false)
const showScreen = ref(false)
/** 缺系统运行库时的"服务器上一键修复"命令（后端装好依赖后为空） */
const qrInstallHint = ref('')
const copiedHint = ref('')
/** 二维码首次展示的时间：用于判断"扫码后迟迟没反应"并给出排查提示 */
const qrShownAt = ref(0)
/** 诊断面板是否展开（默认收起；扫码后 60s 还没登录会自动展开） */
const showDiag = ref(false)
const diagAutoOpen = ref(false)
const now = ref(Date.now())

let timer: number | null = null
let countdown: number | null = null
let pollFast = 0
let slowRetry = 0
/** 已登录且弹窗关闭时的状态查询间隔（后端探测可能让 MCP 新起浏览器，不宜太勤） */
const IDLE_STATUS_PERIOD = 30000
let lastStatusAt = 0

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

/** 二维码展示后已经等了多久（秒）：超过 60s 还没变绿就主动引导排查 */
const qrWaitSeconds = computed(() =>
  qrShownAt.value ? Math.round((now.value - qrShownAt.value) / 1000) : 0)
const scanStuck = computed(() =>
  !!qrImage.value && !loggedIn.value && !qrExpired.value && qrWaitSeconds.value >= 60)
/** 后端正在等扫码（这期间不会去打扰 MCP，所以登录状态靠 cookies 判定） */
const waitingScan = computed(() => !!status.value?.waiting_scan)
/** MCP 侧已判定这次登录会话结束（超时或被新码取代）→ 该重新取码了 */
const scanEnded = computed(() => status.value?.scan_verdict === 'ended')

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
  // 后端查状态最多 8 秒（MCP 忙时），而这里每 2.5 秒轮询一次 → 必须防重入，
  // 否则慢的时候请求会不断堆叠，页面反而更"没反应"。
  if (refreshing.value) return
  refreshing.value = true
  try {
    const resp = await xhsApi.status()
    status.value = resp.data
    error.value = ''
    emit('update:loggedIn', !!resp.data?.logged_in)
  } catch (err: any) {
    error.value = apiErrorMessage(err, '无法获取小红书状态')
    emit('update:loggedIn', false)
  } finally {
    refreshing.value = false
  }
}

/** 后端会把修复命令接在 message 末尾；这里剥掉，改由专门的提示框整块展示 */
function stripHint(msg: string | undefined, hint: string | undefined) {
  const text = (msg || '').trim()
  if (!hint) return text
  return text.replace(hint, '').trim()
}

/** 站点是 http 部署时 navigator.clipboard 不可用（非安全上下文），退回 execCommand */
async function copyText(text: string, tag: string) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const area = document.createElement('textarea')
    area.value = text
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    try { document.execCommand('copy') } catch { /* 都不行就让用户手动选中复制 */ }
    document.body.removeChild(area)
  }
  copiedHint.value = tag
  window.setTimeout(() => { if (copiedHint.value === tag) copiedHint.value = '' }, 1800)
}

function resetQrState() {
  qrImage.value = ''
  qrExpiresAt.value = ''
  qrPending.value = false
  qrPendingMsg.value = ''
  qrWaitSec.value = 0
  qrError.value = ''
  qrLogTail.value = ''
  qrInstallHint.value = ''
  qrShownAt.value = 0
  showDiag.value = false
  diagAutoOpen.value = false
  showLogTail.value = false
  liveState.value = ''
  liveNote.value = ''
  qrSeq.value = 0
  liveScreen.value = null
  showScreen.value = false
  slowRetry = 0
}

/** 开始/继续「实时扫码登录」（服务器自己开浏览器出码） */
async function startLiveLogin() {
  try {
    const resp = await xhsApi.liveLoginStart()
    applyLive(resp.data || {})
    // 只有 qr/verify/waiting/logged_in 才算这条链路可用；unavailable/error 时退回备用方案
    return !['unavailable', 'error'].includes(liveState.value)
  } catch (err: any) {
    qrError.value = apiErrorMessage(err, '启动扫码登录失败')
    liveNote.value = qrError.value
    return false
  }
}

/** 把后端返回的实时状态渲染出来；返回 true 表示这条链路可用 */
function applyLive(data: any) {
  const state = data.state || ''
  if (state === 'unavailable' || state === 'error') {
    // 实时链路不可用（没装 playwright / 浏览器起不来）：退回 MCP 备用方案，
    // 但**原因必须显示出来**，否则用户只会觉得"怎么还是老样子、还是登不上"。
    liveMode.value = false
    liveState.value = state
    liveNote.value = data.message || '实时扫码登录不可用'
    if (data.hint) liveNote.value += `\n${data.hint}`
    return false
  }
  liveMode.value = true
  liveState.value = state
  liveNote.value = data.message || ''
  if (data.image_base64) {
    qrImage.value = `data:${data.mime || 'image/png'};base64,${data.image_base64}`
    qrSeq.value += 1
    qrShownAt.value = qrShownAt.value || Date.now()
    qrError.value = ''
    qrExpiresAt.value = ''            // 实时链路没有"过期"概念：每次探测都是最新的码
    qrPending.value = false
  }
  return true
}

/** 探测实时登录进度（每 2.5 秒一次；每次都会带回最新那张二维码） */
async function probeLive() {
  try {
    const resp = await xhsApi.liveLoginProbe()
    const data = resp.data || {}
    if (data.state === 'closed') {          // 会话没了（被回收/超时）→ 重建
      return await startLiveLogin()
    }
    return applyLive(data)
  } catch (err: any) {
    liveNote.value = apiErrorMessage(err, '登录状态探测失败')
    return true
  }
}

/** 排错：把服务器上那个浏览器的画面抓回来看（扫码没反应时一键定位） */
async function fetchLiveScreen() {
  screenLoading.value = true
  try {
    const resp = await xhsApi.liveLoginDebug(true)
    liveScreen.value = resp.data || null
    showScreen.value = true
  } catch (err: any) {
    liveNote.value = apiErrorMessage(err, '读取服务器浏览器画面失败')
  } finally {
    screenLoading.value = false
  }
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
  // 先查环境（把原因显示出来），再试推荐路径（服务器自己开浏览器出实时码）
  fetchLiveDiag()
  const live = await startLiveLogin()
  if (!live) await fetchQr(true)
}

/** 环境诊断：实时链路为什么不可用（前端会显示出来，也方便远程排错） */
async function fetchLiveDiag() {
  try {
    const resp = await xhsApi.liveLoginAvailable()
    liveAvail.value = resp.data || null
  } catch {
    liveAvail.value = null
  }
}

/** 取二维码：ok=出图；pending=实例启动中（继续轮询）；否则是硬失败
 *
 *  ⚠️ force=true 才向 MCP 重新取码（用户点「重新获取二维码」时）。
 *  绝不能自动重复取码：上游 issue #799 —— 重复调用会新建浏览器、取消旧会话，
 *  用户刚在手机上确认的登录（或设备验证弹窗）会被顶掉，表现为"扫码后毫无反应"。
 */
async function fetchQr(force = false) {
  if (fetchingQr.value) return
  fetchingQr.value = true
  try {
    const resp = await xhsApi.qrcode(force)
    const data = resp.data || {}
    if (data.ok) {
      qrImage.value = `data:${data.mime || 'image/png'};base64,${data.image_base64}`
      qrExpiresAt.value = data.expires_at || ''
      qrPending.value = false
      qrError.value = ''
      qrLogTail.value = ''
      qrInstallHint.value = ''
      if (!data.cached || !qrShownAt.value) qrShownAt.value = Date.now()
      pollFast = 120                       // 接下来 ~5 分钟每 2.5s 查一次登录状态
    } else if (data.pending) {
      qrPending.value = true
      qrInstallHint.value = data.install_hint || ''
      qrPendingMsg.value = stripHint(data.message, qrInstallHint.value) || '小红书实例正在启动…'
      qrWaitSec.value = data.waited || 0
      qrError.value = ''
    } else {
      qrPending.value = false
      qrInstallHint.value = data.install_hint || ''
      qrError.value = stripHint(data.message, qrInstallHint.value)
        || (qrInstallHint.value ? '小红书实例无法启动，请先按下方命令修复服务器' : '未取到二维码')
      qrLogTail.value = data.log_tail || ''
    }
  } catch (err: any) {
    qrPending.value = false
    qrInstallHint.value = ''
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
  if (liveMode.value && liveState.value !== 'logged_in') {
    xhsApi.liveLoginStop().catch(() => {})     // 关掉服务器上的登录浏览器，释放内存
  }
}

/** 换一张新二维码（实时链路=重新打开登录页；MCP 链路=refresh=1） */
async function refreshQr() {
  if (!liveMode.value) return fetchQr(true)
  try {
    const resp = await xhsApi.liveLoginRefresh()
    applyLive(resp.data || {})
  } catch (err: any) {
    qrError.value = apiErrorMessage(err, '刷新二维码失败')
  }
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
    if (!loggedIn.value) {
      await refresh()
    } else if (!showQr.value && Date.now() - lastStatusAt >= IDLE_STATUS_PERIOD) {
      // 已登录且没开弹窗：降频（后端查状态可能让 MCP 新起一个 Chromium，没必要太勤）
      lastStatusAt = Date.now()
      await refresh()
    }

    if (!showQr.value) return
    // ① 实时扫码登录：每次探测都会带回**最新**的二维码（不会扫到过期码），
    //    出现二次验证时后端会告知 verify，登录成功后后端写 cookies 并结束会话。
    if (liveMode.value) {
      if (liveState.value === 'logged_in') return
      const usable = await probeLive()
      if (!usable) await fetchQr(true)          // 实时链路不可用 → 退回 MCP 方案
      return
    }
    // ② MCP 静态二维码（备用）：
    // 启动中/还没出图 → 每次轮询都重试取码（此时还没有登录会话，重复取码无副作用）；
    // 硬失败 → 放慢到每 ~7.5 秒重试一次（避免刷屏）。
    // ⚠️ 一旦出图就**绝不自动重新取码**：重复向 MCP 取码会新建浏览器、取消旧会话
    //    （上游 issue #799），用户刚在手机上确认的登录会被顶掉。
    if (qrPending.value || !qrImage.value) {
      if (!qrError.value || slowRetry++ % 3 === 0) await fetchQr()
    }
    if (scanStuck.value && !diagAutoOpen.value) {
      diagAutoOpen.value = true           // 扫码后 60s 没动静 → 自动展开诊断，别让用户干等
      showDiag.value = true
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
    <!-- 缺浏览器运行库：不会自愈，必须上服务器执行一次修复命令 -->
    <div v-if="status?.install_hint && !loggedIn" class="max-w-[640px] text-left rounded-lg border border-amber-200 bg-amber-50 px-3 py-2">
      <div class="flex items-center justify-between gap-2">
        <span class="text-[11px] font-medium text-amber-800">⚠️ 服务器缺少浏览器运行库，小红书实例起不来（需执行一次修复）</span>
        <button @click="copyText(status.install_hint, 'bar')" class="shrink-0 text-[11px] text-blue-600 hover:underline">
          {{ copiedHint === 'bar' ? '已复制' : '复制命令' }}
        </button>
      </div>
      <pre class="mt-1 max-h-32 overflow-auto bg-white/70 text-[10px] text-gray-700 p-2 rounded whitespace-pre-wrap">{{ status.install_hint }}</pre>
    </div>
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
          <p v-if="qrImage && liveMode" class="mt-1 text-[11px] text-gray-400 text-center">
            这是第 {{ qrSeq }} 次读取的<b>最新</b>二维码（服务器只做"读取"，不会打断你的登录）
          </p>
          <p v-if="qrImage && liveMode" class="mt-1 text-[11px] text-gray-400 text-center">
            二维码变化是小红书页面自己在轮换，属正常；<b>手机上确认后就不要再扫，等 5~10 秒</b>。
            若手机提示「二维码已失效」，点下方「🔄 重新获取二维码」再扫一次即可。
          </p>
          <!-- ② 二次设备安全验证：把那张要扫的码给出来（上游 MCP 做不到这点） -->
          <div v-if="liveMode && liveState === 'verify'" class="mt-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-900 text-center leading-relaxed">
            <b>小红书要求二次安全验证</b><br />
            请再扫上面这张码（这是新的验证码，不是刚才那张），完成后会自动变绿。
          </div>
          <p v-if="!liveMode && qrRemain !== null && qrImage" class="mt-1 text-xs text-center"
             :class="qrExpired ? 'text-amber-600' : 'text-gray-400'">
            {{ qrExpired
              ? '二维码已过期：请点下方「🔄 重新获取二维码」'
              : `二维码有效期剩余 ${qrRemain} 秒` }}
          </p>
          <!-- 为什么没用推荐路径：原因必须显示，否则用户只会觉得"还是登不上" -->
          <div v-if="!liveMode && liveNote" class="mt-2 w-full rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-900 whitespace-pre-wrap">
            <b>当前用的是备用方案（MCP 静态二维码）</b><br />
            {{ liveNote }}
          </div>
          <p v-if="!liveMode && qrExpired && qrImage" class="mt-1 text-[11px] text-amber-600 text-center">
            重新获取会让刚才的扫码失效，请用新二维码再扫一次
          </p>
          <!-- 扫码后没动静：主动引导排查（而不是让用户干等） -->
          <p v-if="scanStuck && !qrError && !scanEnded" class="mt-2 text-[11px] text-amber-700 text-center">
            已经等了 {{ qrWaitSeconds }} 秒还没登录成功 —— 请看下面的排查提示
          </p>
          <!-- MCP 侧已判定"未检测到扫码"：直接说清楚，别让用户继续等 -->
          <p v-if="scanEnded && !loggedIn && !liveMode" class="mt-2 text-[11px] text-amber-700 text-center">
            服务端这次登录会话已结束（未检测到扫码）—— 请点「🔄 重新获取二维码」再扫一次
          </p>
          <p v-if="liveMode && liveState === 'waiting' && !qrImage" class="mt-2 text-[11px] text-gray-500 text-center">
            {{ liveNote || '正在打开登录页…' }}
          </p>
          <p v-if="loggedIn" class="mt-2 text-xs text-green-600 font-medium">✅ 已登录，正在关闭…</p>
        </div>

        <!-- 扫码后无反应：把最常见的原因写清楚 -->
        <div v-if="scanStuck && !qrError && !liveMode" class="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-900 leading-relaxed">
          <p class="font-medium">手机上确认登录了吗？</p>
          <p>① 小红书扫码后，需要在手机上点<b>「确认登录」</b>才算完成。</p>
          <p>② 若手机提示「安全验证 / 请再次扫码」，请点「🔄 重新获取二维码」——
             该走的推荐路径（服务器实时出码）会把那张验证码也显示出来。</p>
          <p>③ 扫码期间请不要反复点刷新：重新取码会让刚才的扫码失效。</p>
        </div>

        <!-- 登录诊断（MCP 原话 + 实例日志尾部）：排查"扫码后没反应"用 -->
        <div v-if="qrImage && !loggedIn" class="mt-3">
          <button @click="showDiag = !showDiag" class="text-[11px] text-blue-600 hover:underline">
            {{ showDiag ? '收起登录诊断' : '登录诊断（MCP 状态 / 实例日志）' }}
          </button>
          <div v-if="showDiag" class="mt-1 rounded-lg bg-gray-50 border border-gray-100 p-2">
            <p class="text-[10px] text-gray-500">实时扫码登录（推荐路径）环境：</p>
            <pre class="text-[10px] text-gray-700 whitespace-pre-wrap break-all">{{ liveAvail
              ? `可用=${liveAvail.ok} 有头=${!liveAvail.headless} DISPLAY=${liveAvail.display || '（无）'}\n浏览器=${liveAvail.browser || '（未找到）'}\n会话=${JSON.stringify(liveAvail.sessions || [])}`
              : '（未获取）' }}</pre>
            <p class="mt-2 text-[10px] text-gray-500">MCP 返回：</p>
            <pre class="text-[10px] text-gray-700 whitespace-pre-wrap break-all">{{ status?.raw || status?.message || '（暂无）' }}</pre>
            <template v-if="status?.log_tail">
              <p class="mt-2 text-[10px] text-gray-500">实例日志尾部（logs/xhs-mcp-*.log）：</p>
              <pre class="max-h-40 overflow-auto bg-gray-900 text-gray-200 text-[10px] p-2 rounded whitespace-pre-wrap">{{ status.log_tail }}</pre>
            </template>
            <p v-if="status?.busy" class="mt-2 text-[10px] text-amber-700">
              MCP 正忙（状态查询超时），稍后会自动重试。
            </p>
          </div>
        </div>

        <!-- 缺浏览器运行库：把服务器上要执行的命令直接给出来（可复制） -->
        <div v-if="qrInstallHint" class="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2">
          <div class="flex items-center justify-between gap-2">
            <span class="text-[11px] font-medium text-amber-800">⚠️ 服务器缺少浏览器运行库（装一次即可，之后扫码正常）</span>
            <button @click="copyText(qrInstallHint, 'qr')" class="shrink-0 text-[11px] text-blue-600 hover:underline">
              {{ copiedHint === 'qr' ? '已复制' : '复制命令' }}
            </button>
          </div>
          <pre class="mt-1 max-h-40 overflow-auto bg-white/70 text-[10px] text-gray-700 p-2 rounded whitespace-pre-wrap">{{ qrInstallHint }}</pre>
        </div>

          <!-- 服务器浏览器画面：扫码"没反应"时点这里，一眼看出服务端到底在显示什么 -->
          <div v-if="liveMode && !loggedIn" class="mt-3 w-full">
            <button
              @click="fetchLiveScreen"
              :disabled="screenLoading"
              class="text-[11px] text-blue-600 hover:underline disabled:opacity-50"
            >{{ screenLoading ? '读取中…' : (showScreen ? '🖥️ 刷新服务器浏览器画面' : '🖥️ 看看服务器浏览器现在显示什么（扫码没反应时点这里）') }}</button>
            <div v-if="showScreen && liveScreen" class="mt-2 rounded-lg border border-gray-200 bg-gray-50 p-2">
              <img v-if="liveScreen.image_base64" :src="`data:image/png;base64,${liveScreen.image_base64}`"
                   alt="服务器浏览器画面" class="w-full rounded border border-gray-200" />
              <p class="mt-1 text-[10px] text-gray-500 break-all">URL: {{ liveScreen.url }}</p>
              <p class="text-[10px] text-gray-500">标题：{{ liveScreen.title }}</p>
              <p class="text-[10px] text-gray-500">
                元素：{{ JSON.stringify(liveScreen.selectors) }}
                · web_session 已换新：{{ liveScreen.web_session_changed }}
              </p>
              <pre class="mt-1 max-h-32 overflow-auto text-[10px] text-gray-700 whitespace-pre-wrap break-all">{{ (liveScreen.text || '').slice(0, 600) }}</pre>
              <template v-if="liveScreen.failed_requests?.length">
                <p class="mt-1 text-[10px] text-gray-500">加载失败的请求（可能是导致登录卡住的原因）：</p>
                <pre class="max-h-24 overflow-auto text-[10px] text-amber-700 whitespace-pre-wrap break-all">{{ liveScreen.failed_requests.join('\n') }}</pre>
              </template>
              <pre v-if="liveScreen.console?.length" class="mt-1 max-h-24 overflow-auto bg-gray-900 text-gray-200 text-[10px] p-2 rounded whitespace-pre-wrap">{{ liveScreen.console.join('\n') }}</pre>
              <p v-if="liveScreen.error" class="mt-1 text-[10px] text-red-600">读取出错：{{ liveScreen.error }}</p>
            </div>
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
            @click="refreshQr"
            :disabled="fetchingQr"
            :title="liveMode
              ? '重新打开登录页，换一张最新二维码'
              : '重新向小红书要一张新二维码（会让上一次的扫码失效，只在二维码过期或确定没扫过时点）'"
            class="px-3 py-1.5 text-xs rounded-lg bg-blue-600 text-white hover:bg-blue-700 transition disabled:opacity-50"
          >{{ fetchingQr ? '获取中…' : '🔄 重新获取二维码' }}</button>
          <button
            v-if="!liveMode"
            @click="fileInput?.click()"
            :disabled="importing"
            class="px-3 py-1.5 text-xs rounded-lg bg-white border border-gray-200 text-gray-600 hover:bg-gray-50 transition disabled:opacity-50"
            title="备用方案：把本机登录好的 cookies.json 导进来（一般不需要）"
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
          <template v-if="liveMode">
            二维码由服务器上的浏览器<b>实时</b>读取（每 2.5 秒刷新），扫码后请在手机上点「确认登录」。<br />
            若小红书弹出「安全验证 / 请再次扫码」，本页会把那张码也显示出来 —— 再扫一次即可。<br />
          </template>
          <template v-else>
            首次登录时服务器要下载无头浏览器（约 150MB），可能等 1~2 分钟，属正常现象。<br />
            扫码后请在手机上点「确认登录」，并<b>不要</b>刷新二维码（刷新会让这次扫码失效）。<br />
          </template>
          登录状态只保存在你自己的账号目录里，与其他用户互不影响；同一个账号不要同时在别处登录网页版。
        </p>
      </div>
    </div>
  </div>
</template>
