<script setup lang="ts">
/**
 * 小红书登录状态条 + 扫码登录弹窗（放在「生成旅游攻略」标题旁边）
 *
 * 登录方式（用户视角只有一种：扫码）：
 *   点「登录小红书」→ 后端调 MCP 的 get_login_qrcode → 网页弹出二维码
 *   → 用户用小红书 App 扫码 → 前端每 2.5 秒查一次状态 → 变绿、弹窗自动关闭。
 *
 * 服务器上不需要桌面环境、不需要弹浏览器，用户也不需要下载/上传任何文件。
 * 「导入 cookies.json」只在需要**迁移已有登录态**时使用（放在弹窗里的次要入口）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { apiErrorMessage, xhsApi } from '@/lib/api'

const emit = defineEmits<{
  'update:loggedIn': [value: boolean]
}>()

const status = ref<any>(null)
const loading = ref(false)
const fetchingQr = ref(false)
const importing = ref(false)
const clearing = ref(false)
const error = ref('')
const notice = ref('')
const fileInput = ref<HTMLInputElement | null>(null)

/** 二维码弹窗 */
const showQr = ref(false)
const qrImage = ref('')
const qrText = ref('')
const qrExpiresAt = ref('')
const now = ref(Date.now())

let timer: number | null = null
let countdown: number | null = null
let pollFast = 0

const loggedIn = computed(() => !!status.value?.logged_in)
const running = computed(() => !!status.value?.mcp_running)
const exeOk = computed(() => status.value?.exe_available !== false)
const platform = computed(() => status.value?.platform || '')
const isServerLinux = computed(() => platform.value && platform.value !== 'win32')

/** 二维码剩余秒数（后端从 MCP 提示语里解析出的过期时间） */
const qrRemain = computed(() => {
  if (!qrExpiresAt.value) return null
  const ms = new Date(qrExpiresAt.value).getTime() - now.value
  return Math.max(0, Math.round(ms / 1000))
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
    ? '服务器上缺少 Linux 版小红书服务：按 docs/xhs-multi-user.md 第五节放好 Linux 构建（二进制会自动下载浏览器）'
    : '缺少小红书服务可执行文件：请查看 .env 与 xiaohongshumcp/ 目录'
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

/** 打开弹窗并取二维码 */
async function openQr() {
  showQr.value = true
  notice.value = ''
  if (loggedIn.value) {
    // 已登录时先问一句是否换号（避免误点把当前登录态顶掉）
    if (!confirm('当前已登录。重新扫码会替换为新的账号，确定继续吗？')) {
      showQr.value = false
      return
    }
    await handleClear(false)
  }
  await fetchQr()
}

async function fetchQr() {
  fetchingQr.value = true
  error.value = ''
  try {
    const resp = await xhsApi.qrcode()
    const data = resp.data || {}
    if (data.ok) {
      qrImage.value = `data:${data.mime || 'image/png'};base64,${data.image_base64}`
      qrText.value = data.text || ''
      qrExpiresAt.value = data.expires_at || ''
      pollFast = 120                       // 接下来 ~5 分钟每 2.5s 查一次状态
    } else {
      qrImage.value = ''
      error.value = data.message || '未取到二维码'
    }
  } catch (err: any) {
    error.value = apiErrorMessage(err, '获取二维码失败')
  } finally {
    fetchingQr.value = false
  }
}

/** 退出登录（换号） */
async function handleClear(reopen = true) {
  clearing.value = true
  error.value = ''
  notice.value = ''
  try {
    const resp = await xhsApi.clear()
    notice.value = resp.data?.message || '已退出登录'
    await refresh()
    if (reopen) await fetchQr()
  } catch (err: any) {
    error.value = apiErrorMessage(err, '退出登录失败')
  } finally {
    clearing.value = false
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
      pollFast = 8
      if (loggedIn.value) showQr.value = false
    } else {
      error.value = resp.data?.message || '导入失败'
    }
  } catch (err: any) {
    error.value = apiErrorMessage(err, '导入 cookies.json 失败')
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
  timer = window.setInterval(() => {
    if (pollFast > 0) pollFast -= 1
    if (pollFast > 0 || !loggedIn.value) refresh()
    // 扫码成功 → 自动关弹窗
    if (loggedIn.value && showQr.value) {
      showQr.value = false
      notice.value = `已登录${status.value?.username ? '：' + status.value.username : ''}`
    }
    // 二维码过期 → 自动刷新（用户仍在弹窗里等）
    if (showQr.value && qrExpired.value && !fetchingQr.value) fetchQr()
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
          <span class="relative inline-flex rounded-full h-2.5 w-2.5" :class="dotClass"></span>
        </span>
        <span class="font-medium shrink-0">小红书</span>
        <span class="truncate">{{ statusText }}</span>
      </div>

      <button
        @click="openQr"
        :disabled="fetchingQr"
        :title="loggedIn ? '换一个账号登录（重新扫码）' : '打开二维码，用小红书 App 扫码登录'"
        class="px-3 py-1.5 text-xs rounded-lg transition disabled:opacity-50 flex items-center gap-1.5 shrink-0"
        :class="loggedIn ? 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50' : 'bg-red-500 text-white hover:bg-red-600'"
      >
        <span v-if="fetchingQr" class="inline-block w-3 h-3 border-2 border-current border-t-transparent rounded-full animate-spin"></span>
        {{ fetchingQr ? '正在获取二维码…' : (loggedIn ? '🔄 换个账号' : '🔐 登录小红书') }}
      </button>

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
            <img v-if="qrImage" :src="qrImage" alt="登录二维码" class="w-full h-full object-contain" />
            <span v-else class="text-xs text-gray-400 px-4 text-center">
              {{ fetchingQr ? '正在获取二维码…' : (error || '二维码不可用') }}
            </span>
          </div>

          <p class="mt-3 text-xs text-gray-500 text-center">
            打开小红书 App →「我」→ 右上角扫一扫
          </p>
          <p v-if="qrRemain !== null" class="mt-1 text-xs" :class="qrExpired ? 'text-amber-600' : 'text-gray-400'">
            {{ qrExpired ? '二维码已过期，正在自动刷新…' : `二维码有效期剩余 ${qrRemain} 秒` }}
          </p>
          <p v-if="loggedIn" class="mt-2 text-xs text-green-600 font-medium">✅ 已登录，正在关闭…</p>
        </div>

        <div class="mt-4 flex flex-wrap items-center gap-2 justify-center">
          <button
            @click="fetchQr"
            :disabled="fetchingQr"
            class="px-3 py-1.5 text-xs rounded-lg bg-blue-600 text-white hover:bg-blue-700 transition disabled:opacity-50"
          >🔄 刷新二维码</button>
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
        <p v-if="error" class="mt-3 text-xs text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2 whitespace-pre-wrap">{{ error }}</p>
        <p class="mt-3 text-[11px] text-gray-400 leading-relaxed">
          提示：登录状态只保存在你自己的账号目录里（<code>xiaohongshumcp/users/&lt;你的 user_id&gt;/</code>），
          与其他用户互不影响。<br />
          同一个小红书账号不要在别处再登录网页版，否则会把这里的登录态顶下线。
        </p>
      </div>
    </div>
  </div>
</template>
