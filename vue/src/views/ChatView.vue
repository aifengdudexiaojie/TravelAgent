<script setup lang="ts">
/**
 * 聊天页
 *
 * 三种模式（互斥，选择结果持久化在 localStorage['chat_mode']）：
 *   💬 普通     —— 不检索攻略，直接回答
 *   🧠 历史模式 —— 关键词 + 意图判定，判定需要时**只检索我自己**的攻略
 *   🌐 公开模式 —— 不做意图判定，**检索「我的 + 他人已公开」**的攻略作为参考
 */
import { ref, nextTick, onMounted, watch } from 'vue'
import { chatApi, streamChat, type ChatMode } from '@/lib/api'

interface RagDecision {
  need_rag?: boolean
  reason?: string
  intent_type?: string
  source?: string
  query?: string
  keywords?: string[]
  mode?: string
  visibility?: string
}

interface GuideRef {
  guide_id?: string
  title?: string
  destination?: string
  score?: number
  is_mine?: boolean | null
  is_public?: boolean
  author?: string
}

interface Message {
  role: 'user' | 'assistant'
  content: string
  used_rag?: boolean
  chat_mode?: ChatMode
  rag_decision?: RagDecision | null
  related_guides?: GuideRef[]
}

const MODES: { value: ChatMode; label: string; icon: string; hint: string }[] = [
  { value: 'normal', label: '普通', icon: '💬', hint: '不检索攻略，直接由模型回答' },
  { value: 'history', label: '历史模式', icon: '🧠', hint: '只检索「我自己」的攻略' },
  { value: 'public', label: '公开模式', icon: '🌐', hint: '检索「我的 + 他人已公开」的攻略' },
]

const messages = ref<Message[]>([])
const input = ref('')
const loading = ref(false)
const error = ref('')
const chatContainer = ref<HTMLElement | null>(null)

/** 模式选择（兼容旧的 chat_history_mode 开关） */
function initialMode(): ChatMode {
  const saved = localStorage.getItem('chat_mode')
  if (saved === 'normal' || saved === 'history' || saved === 'public') return saved
  return localStorage.getItem('chat_history_mode') === '1' ? 'history' : 'normal'
}
const chatMode = ref<ChatMode>(initialMode())
watch(chatMode, (val) => {
  localStorage.setItem('chat_mode', val)
  localStorage.setItem('chat_history_mode', val === 'history' ? '1' : '0')
})

const currentMode = () => MODES.find((m) => m.value === chatMode.value) || MODES[0]

onMounted(async () => {
  try {
    const resp = await chatApi.getHistory(20)
    if (resp.data.messages?.length) {
      messages.value = resp.data.messages.map((m: any) => ({
        role: m.role,
        content: m.content,
      }))
      await scrollToBottom()
    }
  } catch {}
})

async function sendMessage() {
  const text = input.value.trim()
  if (!text || loading.value) return

  const modeAtSend = chatMode.value
  messages.value.push({ role: 'user', content: text })
  input.value = ''
  loading.value = true
  error.value = ''
  await scrollToBottom()

  const history = messages.value.slice(-10).map((m) => ({ role: m.role, content: m.content })).slice(0, -1)
  messages.value.push({ role: 'assistant', content: '', chat_mode: modeAtSend })
  const assistantMsg = messages.value[messages.value.length - 1] as Message

  try {
    const data = await streamChat(
      text,
      history,
      (event) => {
        if (event.type === 'chunk') {
          assistantMsg.content += event.data
          scrollToBottom()
        } else if (event.type === 'start') {
          // 判定结果先到，立刻展示（提升等待期体感）
          applyMeta(assistantMsg, event.data, modeAtSend)
        } else if (event.type === 'error') {
          error.value = event.data?.message || '发送失败'
          if (!assistantMsg.content) {
            assistantMsg.content = '抱歉，我暂时无法回答，请稍后再试。'
          }
        } else if (event.type === 'done') {
          applyMeta(assistantMsg, event.data, modeAtSend)
          if (!assistantMsg.content && event.data?.reply) {
            assistantMsg.content = event.data.reply
          }
        }
      },
      { chatMode: modeAtSend },
    )
    if (data?.reply && !assistantMsg.content) {
      assistantMsg.content = data.reply
    }
    if (data) applyMeta(assistantMsg, data, modeAtSend)
  } catch (err: any) {
    error.value = err?.message || err?.response?.data?.detail || '发送失败'
    if (!assistantMsg.content) {
      assistantMsg.content = '抱歉，我暂时无法回答，请稍后再试。'
    }
  } finally {
    loading.value = false
    await scrollToBottom()
  }
}

function applyMeta(msg: Message, data: any, fallbackMode: ChatMode) {
  msg.chat_mode = (data?.chat_mode as ChatMode) || (data?.history_mode ? 'history' : fallbackMode)
  msg.used_rag = data?.used_rag
  msg.rag_decision = data?.rag_decision
  msg.related_guides = data?.related_guides || []
}

async function scrollToBottom() {
  await nextTick()
  if (chatContainer.value) {
    chatContainer.value.scrollTop = chatContainer.value.scrollHeight
  }
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
  }
}

/** 判定结果文案（按模式区分） */
function decisionText(msg: Message): string {
  const d = msg.rag_decision
  const mine = (msg.related_guides || []).filter((g) => g.is_mine).length
  const others = (msg.related_guides || []).length - mine

  if (msg.used_rag) {
    if (msg.chat_mode === 'public') {
      return others > 0
        ? `检索到 ${mine} 条我的 + ${others} 条公开攻略`
        : `检索到 ${mine} 条我的攻略`
    }
    return `检索到 ${msg.related_guides?.length || 0} 条我的历史攻略`
  }
  if (msg.chat_mode === 'public') {
    return `未检索到相关攻略${d?.reason ? '：' + d.reason : ''}`
  }
  return `未触发检索${d?.reason ? '：' + d.reason : ''}`
}

function guideLabel(g: GuideRef): string {
  const base = g.title || g.destination || '未命名攻略'
  if (g.is_mine) return base
  return `${base}（公开${g.author ? '·' + g.author : ''}）`
}
</script>

<template>
  <div class="flex flex-col h-[calc(100vh-140px)]">
    <!-- 模式说明 -->
    <div
      :class="[
        'mb-3 p-3 border rounded-lg text-sm transition',
        chatMode === 'history'
          ? 'bg-amber-50 border-amber-200 text-amber-800'
          : chatMode === 'public'
            ? 'bg-emerald-50 border-emerald-200 text-emerald-800'
            : 'bg-blue-50 border-blue-100 text-blue-700',
      ]"
    >
      <template v-if="chatMode === 'history'">
        🧠 <b>历史模式</b>：先做关键词 + 意图判定，判断你在问自己的历史行程时，
        只检索<b>你自己</b>保存过的攻略作为参考。
      </template>
      <template v-else-if="chatMode === 'public'">
        🌐 <b>公开模式</b>：每次提问都会检索攻略库 —— <b>你自己的 + 其他人公开分享的</b>，
        用它们来回答（引用别人的攻略时会说明来源，不会当成你的经历）。
      </template>
      <template v-else>
        💬 <b>普通模式</b>：不检索任何攻略，直接由模型回答。
        想结合攻略回答时，切到「历史模式」（只查自己的）或「公开模式」（自己的 + 公开的）。
      </template>
    </div>

    <!-- 消息列表 -->
    <div ref="chatContainer" class="flex-1 overflow-y-auto space-y-4 mb-4 pr-2">
      <div v-if="messages.length === 0" class="text-center text-gray-400 mt-20">
        <p class="text-4xl mb-4">💬</p>
        <p>开始聊天吧！问我关于旅游的任何问题</p>
      </div>

      <div
        v-for="(msg, idx) in messages"
        :key="idx"
        :class="['flex', msg.role === 'user' ? 'justify-end' : 'justify-start']"
      >
        <div
          :class="[
            'max-w-[80%] rounded-2xl px-4 py-3',
            msg.role === 'user'
              ? 'bg-blue-600 text-white'
              : 'bg-white border border-gray-100 text-gray-800'
          ]"
        >
          <p class="text-sm whitespace-pre-wrap leading-relaxed">{{ msg.content }}</p>

          <!-- 模式判定与引用（仅助手消息，且非普通模式） -->
          <div
            v-if="msg.role === 'assistant' && msg.chat_mode && msg.chat_mode !== 'normal'"
            class="mt-2 pt-2 border-t border-gray-100"
          >
            <p
              class="text-xs"
              :class="msg.used_rag
                ? 'text-green-600'
                : (msg.chat_mode === 'history' ? 'text-gray-400' : 'text-gray-400')"
            >
              {{ msg.used_rag ? '📚 ' : '🧭 ' }}{{ msg.chat_mode === 'history' ? '历史模式' : '公开模式' }}
              · {{ decisionText(msg) }}
            </p>
            <div v-if="msg.used_rag && msg.related_guides?.length" class="flex flex-wrap gap-1 mt-1">
              <span
                v-for="guide in msg.related_guides"
                :key="guide.guide_id"
                class="text-xs px-2 py-0.5 rounded-full"
                :class="guide.is_mine ? 'bg-blue-50 text-blue-600' : 'bg-emerald-50 text-emerald-700'"
                :title="`得分 ${guide.score ?? ''}`"
              >
                {{ guideLabel(guide) }}
              </span>
            </div>
            <p v-if="msg.rag_decision?.query" class="text-[11px] text-gray-400 mt-1">
              检索语句：{{ msg.rag_decision.query }}
            </p>
          </div>
        </div>
      </div>

      <!-- 加载指示 -->
      <div v-if="loading" class="flex justify-start">
        <div class="bg-white border border-gray-100 rounded-2xl px-4 py-3">
          <div class="flex items-center gap-2">
            <div class="flex gap-1">
              <span class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay: 0ms"></span>
              <span class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay: 150ms"></span>
              <span class="w-2 h-2 bg-gray-400 rounded-full animate-bounce" style="animation-delay: 300ms"></span>
            </div>
            <span class="text-xs text-gray-400">
              {{ chatMode === 'public' ? '正在检索攻略库…' : (chatMode === 'history' ? '正在判定并检索…' : '思考中…') }}
            </span>
          </div>
        </div>
      </div>
    </div>

    <!-- 错误提示 -->
    <div v-if="error" class="mb-2 p-2 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">
      {{ error }}
    </div>

    <!-- 模式选择 -->
    <div class="mb-2 flex flex-wrap items-center gap-2">
      <div class="flex rounded-xl border border-gray-200 bg-white p-1">
        <button
          v-for="m in MODES"
          :key="m.value"
          @click="chatMode = m.value"
          :disabled="loading"
          :title="m.hint"
          :class="[
            'px-3 py-1.5 text-xs rounded-lg transition disabled:opacity-50',
            chatMode === m.value
              ? (m.value === 'history'
                  ? 'bg-amber-500 text-white'
                  : m.value === 'public' ? 'bg-emerald-600 text-white' : 'bg-blue-600 text-white')
              : 'text-gray-600 hover:bg-gray-100',
          ]"
        >
          {{ m.icon }} {{ m.label }}
        </button>
      </div>
      <span class="text-xs text-gray-400">{{ currentMode().hint }}</span>
    </div>

    <!-- 输入区 -->
    <div class="flex gap-2">
      <textarea
        v-model="input"
        @keydown="handleKeydown"
        :placeholder="chatMode === 'history'
          ? '例如：上次去西安那个能看长恨歌的地方叫什么来着？（Enter 发送，Shift+Enter 换行）'
          : chatMode === 'public'
            ? '例如：乌镇西栅住哪里、怎么安排夜景？（会检索我的 + 公开攻略）'
            : '输入你的旅游问题...（Enter 发送，Shift+Enter 换行）'"
        rows="1"
        class="flex-1 px-4 py-3 border border-gray-200 rounded-xl focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none resize-none text-sm"
      />
      <button
        @click="sendMessage"
        :disabled="loading || !input.trim()"
        class="px-6 py-3 bg-blue-600 hover:bg-blue-700 text-white rounded-xl transition disabled:opacity-50 text-sm font-medium"
      >
        发送
      </button>
    </div>
  </div>
</template>
