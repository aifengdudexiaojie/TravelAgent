import { defineStore } from 'pinia'
import { apiErrorMessage, recognizeIntent, streamSummary, summaryApi } from '@/lib/api'
import { useAuthStore } from '@/stores/auth'
import type { IntentContent, ProgressEvent, FinalSummary } from '@/types'

/**
 * 攻略生成流程状态机（**唯一状态源**）
 * ================================================================================
 *  input           输入需求
 *  recognizing     意图识别中（loading）
 *  intent_confirm  展示意图，等待确认 / 改完再重新识别
 *  analyzing       分析中（SSE 进度）
 *  done            分析完成，展示最终攻略
 *
 * 为什么状态放 store 而不是组件里：
 *   组件随路由切换被销毁，进度就会"停了 + 重置"。放这里之后：
 *   · 切到别的页面再切回来 → 进度还在，SSE 也一直在收；
 *   · 页面刷新 → 从 localStorage 恢复视图，并重新挂到后端同一个 task_id 上追平进度
 *     （后端 /api/summary/stream 会重放全部历史事件，同一个 task_id 只分析一次）。
 */
export type Stage = 'input' | 'recognizing' | 'intent_confirm' | 'analyzing' | 'done'

/** RAG 入库状态 */
export type SaveState = 'idle' | 'saving' | 'saved' | 'failed'

const STORAGE_KEY = 'plan_state_v2'
const STATE_TTL_MS = 6 * 60 * 60 * 1000      // 超过 6 小时的旧状态直接丢弃
const MAX_EVENTS = 1500                       // 前端事件缓冲上限（防长任务爆内存）
const PERSIST_INTERVAL_MS = 1000              // 事件流水持久化节流

interface PersistedState {
  ts: number
  stage: Stage
  userQuery: string
  taskId: number | null
  intent: IntentContent | null
  events: ProgressEvent[]
  finalSummary: FinalSummary | null
  saveState: SaveState
  savedGuide: { guide_id?: string; title?: string } | null
}

function loadPersisted(): PersistedState | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as PersistedState
    if (!parsed?.ts || Date.now() - parsed.ts > STATE_TTL_MS) {
      localStorage.removeItem(STORAGE_KEY)
      return null
    }
    return parsed
  } catch {
    return null
  }
}

const persisted = loadPersisted()

/**
 * 当前登录用户身份。
 * 后端在 SSE 流程结束后会把最终总结自动写入 RAG，归属就取这里的 user_id；
 * 不带的话会落到环境变量默认归属（rag_test_user），
 * 结果就是"聊天历史模式检索不到自己刚生成的攻略"。
 */
function currentOwner() {
  try {
    const auth = useAuthStore()
    const user = auth.user || {}
    return { user_id: user.user_id, username: user.username, nickname: user.nickname }
  } catch {
    return {}
  }
}

export const usePlanStore = defineStore('plan', {
  state: () => ({
    stage: (persisted?.stage ?? 'input') as Stage,
    userQuery: persisted?.userQuery ?? '',

    // 阶段1：意图识别
    taskId: (persisted?.taskId ?? null) as number | null,
    intent: (persisted?.intent ?? null) as IntentContent | null,
    isRecognizing: false,

    // 阶段2：分析进度
    events: (persisted?.events ?? []) as ProgressEvent[],
    isAnalyzing: false,
    /** SSE 订阅是否活着（用于判断"回到页面时要不要重新挂载"） */
    streamLive: false,
    /** RAG 入库进度 */
    saveState: (persisted?.saveState ?? 'idle') as SaveState,
    saveReport: null as any,
    validationError: null as any,
    savedGuide: (persisted?.savedGuide ?? null) as { guide_id?: string; title?: string } | null,

    // 阶段3：结果
    finalSummary: (persisted?.finalSummary ?? null) as FinalSummary | null,
    error: '',
    notice: '',
  }),

  getters: {
    /** 已分析完成的地点 */
    addressDone(state): number {
      return state.events.filter((e) => e.type === 'address_done').length
    },
    /** 总地点数（从带 total 的事件里取） */
    addressTotal(state): number {
      const last = [...state.events].reverse().find((e) => e.data?.total)
      return last?.data?.total || 0
    },
    /** 一句话进度摘要（侧边栏/标题用） */
    progressText(state): string {
      if (state.stage === 'recognizing') return '正在识别旅行意图…'
      if (state.stage === 'analyzing') {
        const done = state.events.filter((e) => e.type === 'address_done').length
        const total = [...state.events].reverse().find((e) => e.data?.total)?.data?.total || 0
        return total ? `分析中 ${done}/${total} 个地点` : '正在准备分析…'
      }
      if (state.stage === 'done') return '攻略生成完成'
      return ''
    },
    busy(state): boolean {
      return state.isRecognizing || state.isAnalyzing
    },
  },

  actions: {
    // ----------------------------------------------------------------
    // 持久化
    // ----------------------------------------------------------------
    persist() {
      try {
        const payload: PersistedState = {
          ts: Date.now(),
          stage: this.stage,
          userQuery: this.userQuery,
          taskId: this.taskId,
          intent: this.intent,
          events: this.events.slice(-MAX_EVENTS),
          finalSummary: this.finalSummary,
          saveState: this.saveState,
          savedGuide: this.savedGuide,
        }
        localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
      } catch {
        /* 存储满/隐私模式：忽略，不影响功能 */
      }
    },

    setQuery(query: string) {
      this.userQuery = query
      this.persist()
    },

    // ----------------------------------------------------------------
    // 阶段1：意图识别（可反复调用 = 重新识别）
    // ----------------------------------------------------------------
    /**
     * 识别意图。每次调用都会**真正重新识别**：
     * 先清掉上一次的意图（避免界面上还显示旧结果），再请求后端。
     */
    async recognize(query?: string) {
      const text = (query ?? this.userQuery).trim()
      if (!text) {
        this.error = '请先输入你的旅行计划'
        return
      }
      this.userQuery = text
      this.intent = null            // 关键：清空旧意图，杜绝"改了输入还是老结果"
      this.taskId = null
      this.error = ''
      this.notice = ''
      this.stage = 'recognizing'
      this.isRecognizing = true
      try {
        const data = await recognizeIntent(text)
        this.taskId = data?.task_id ?? null
        this.intent = data?.intent_content ?? null
        if (!this.taskId || !this.intent) {
          throw new Error('意图识别返回为空，请重试')
        }
        this.stage = 'intent_confirm'
      } catch (err: any) {
        this.error = apiErrorMessage(err, '意图识别失败，请重试')
        this.stage = 'input'
      } finally {
        this.isRecognizing = false
        this.persist()
      }
    },

    /** 回到输入阶段（保留输入内容，方便改一改再识别） */
    redo() {
      this.stage = 'input'
      this.intent = null
      this.taskId = null
      this.error = ''
      this.notice = ''
      this.persist()
    },

    // ----------------------------------------------------------------
    // 阶段2：分析（后端后台任务 + 可重放订阅）
    // ----------------------------------------------------------------
    /** 确认意图 → 开始分析 */
    async analyze() {
      if (!this.taskId) {
        this.error = '缺少任务号，请重新识别意图'
        this.stage = 'input'
        return
      }
      this.stage = 'analyzing'
      this.events = []
      this.finalSummary = null
      this.saveState = 'idle'
      this.saveReport = null
      this.validationError = null
      this.savedGuide = null
      this.error = ''
      this.notice = ''
      await this._attach()
    },

    /**
     * 重新挂到后端任务上（刷新页面 / 切页回来后调用）。
     * fromIndex=0 → 后端重放全部历史事件，所以进度能追平而不是从 0 开始。
     */
    async resume() {
      if (this.stage !== 'analyzing' || !this.taskId) return
      if (this.streamLive) return              // 订阅还活着，什么都不用做
      this.notice = '正在重新连接后端分析任务…'
      try {
        await summaryApi.task(this.taskId)
      } catch (err: any) {
        if (err?.response?.status === 404) {
          this.error = '上次的分析任务已失效（后端可能重启过），请重新识别后再开始'
          this.resetToInput()
          return
        }
      }
      await this._attach()
    },

    /** 订阅后端进度（内部方法） */
    async _attach() {
      if (!this.taskId || this.streamLive) return
      this.streamLive = true
      this.isAnalyzing = true
      let lastPersist = 0
      try {
        const result = await streamSummary(
          this.taskId,
          (event) => {
            this.handleEvent(event)
            const now = Date.now()
            if (now - lastPersist > PERSIST_INTERVAL_MS) {
              lastPersist = now
              this.persist()
            }
          },
          { fromIndex: 0, owner: currentOwner() },
        )
        if (result && !this.finalSummary) {
          this.finalSummary = result as FinalSummary
          this.stage = 'done'
        }
        if (this.stage === 'analyzing') this.stage = 'done'
      } catch (err: any) {
        this.error = err?.message || '分析中断，可重试'
        if (this.stage === 'analyzing') this.stage = 'intent_confirm'
      } finally {
        this.streamLive = false
        this.isAnalyzing = false
        this.notice = ''
        this.persist()
      }
    },

    /** 处理单个 SSE 事件 */
    handleEvent(event: any) {
      if (!event || event.type === 'ping') return
      this.events.push(event)
      if (this.events.length > MAX_EVENTS) {
        this.events.splice(0, this.events.length - MAX_EVENTS)
      }
      switch (event.type) {
        case 'done':
          this.finalSummary = event.data as FinalSummary
          this.stage = 'done'
          break
        case 'saving':
          this.saveState = 'saving'
          break
        case 'saved':
          this.saveState = 'saved'
          this.saveReport = event.data
          break
        case 'validation_error':
          this.saveState = 'failed'
          this.validationError = event.data
          break
        case 'save_error':
          this.saveState = 'failed'
          this.validationError = { kind: 'save', message: event.data?.message }
          break
        case 'error':
          this.error = event.data?.message || '分析失败'
          break
      }
    },

    /** 取消正在跑的分析 */
    async cancelAnalyze() {
      if (!this.taskId) return
      try {
        await summaryApi.cancel(this.taskId)
      } catch {
        /* 任务可能已结束 */
      }
      this.isAnalyzing = false
      this.streamLive = false
      this.stage = 'intent_confirm'
      this.notice = '已取消本次分析'
      this.persist()
    },

    /** 完成保存后记录归属 */
    markSaved(guide: { guide_id?: string; title?: string } | null) {
      this.savedGuide = guide
      this.persist()
    },

    // ----------------------------------------------------------------
    // 重置
    // ----------------------------------------------------------------
    resetToInput() {
      this.stage = 'input'
      this.taskId = null
      this.intent = null
      this.isRecognizing = false
      this.isAnalyzing = false
      this.streamLive = false
      this.events = []
      this.finalSummary = null
      this.saveState = 'idle'
      this.saveReport = null
      this.validationError = null
      this.savedGuide = null
      this.error = ''
      this.notice = ''
      this.persist()
    },

    /** 清空所有内容（含输入框） */
    resetAll() {
      this.userQuery = ''
      this.resetToInput()
    },
  },
})
