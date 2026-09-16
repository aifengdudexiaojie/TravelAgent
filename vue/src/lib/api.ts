/**
 * 后端 API 客户端
 *
 * 约定：
 *  - axios 封装的接口返回 AxiosResponse（调用方用 resp.data 取值）
 *  - SSE 封装（streamChat / streamSummary）返回最终 done 事件的数据对象
 */
import axios from 'axios'
import type { AxiosResponse } from 'axios'

export const http = axios.create({
  baseURL: '/api',
  timeout: 120000,
})

// 请求注入 token
http.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 401 时清理本地登录态
http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    if (error?.response?.status === 401) {
      localStorage.removeItem('token')
      localStorage.removeItem('user')
    }
    return Promise.reject(error)
  },
)

/** 常见校验字段的中文名（用于把 422 的字段名翻成人话） */
const FIELD_LABELS: Record<string, string> = {
  rating_text: '评价内容',
  rating: '评分',
  title: '标题',
  destination: '目的地',
  message: '消息',
  query: '问题',
}

/**
 * 把 axios / fetch 的错误转成可以直接显示的一行文字。
 *
 * 后端 FastAPI 的校验错误是 422 且 `detail` 为**对象数组**
 * （例如 rating_text 少于 10 字 → [{type:'string_too_short', loc:[...], msg:'...'}]）。
 * 直接渲染数组会变成 "[object Object]"，所以这里统一提取成人话。
 */
export function apiErrorMessage(err: any, fallback = '操作失败，请稍后重试'): string {
  const detail = err?.response?.data?.detail

  if (typeof detail === 'string' && detail.trim()) return detail

  if (Array.isArray(detail)) {
    const parts = detail.map((item: any) => {
      if (typeof item === 'string') return item
      const rawField = Array.isArray(item?.loc) ? item.loc[item.loc.length - 1] : ''
      const field = FIELD_LABELS[rawField] || rawField
      let msg = String(item?.msg ?? JSON.stringify(item))
      if (item?.type === 'string_too_short' && item?.ctx?.min_length != null) {
        msg = `至少需要 ${item.ctx.min_length} 个字`
      } else if (item?.type === 'string_too_long' && item?.ctx?.max_length != null) {
        msg = `最多 ${item.ctx.max_length} 个字`
      } else if (item?.type === 'less_than_equal' && item?.ctx?.le != null) {
        msg = `不能大于 ${item.ctx.le}`
      } else if (item?.type === 'greater_than_equal' && item?.ctx?.ge != null) {
        msg = `不能小于 ${item.ctx.ge}`
      } else if (item?.type === 'missing') {
        msg = '缺少必填项'
      }
      return field ? `${field}：${msg}` : msg
    })
    if (parts.length) return parts.join('；')
  }

  if (typeof err?.response?.data?.message === 'string') return err.response.data.message
  return err?.message || fallback
}

// ================================================================
// 认证
// ================================================================
export const authApi = {
  login(username: string, password: string): Promise<AxiosResponse> {
    return http.post('/auth/login', { username, password })
  },
  register(username: string, password: string, nickname?: string): Promise<AxiosResponse> {
    return http.post('/auth/register', { username, password, nickname })
  },
}

// ================================================================
// 聊天
// ================================================================
/** 聊天模式：普通 / 历史（只检索自己的攻略）/ 公开（自己的 + 他人公开的） */
export type ChatMode = 'normal' | 'history' | 'public'

export interface ChatStreamOptions {
  sessionId?: string
  requestId?: string
  /** 聊天模式（推荐直接用这个） */
  chatMode?: ChatMode
  /** 兼容字段：true 等价于 chatMode='history'（仅在未传 chatMode 时生效） */
  historyMode?: boolean
}

export const chatApi = {
  /** 非流式发送 */
  sendMessage(payload: {
    message: string
    session_id?: string
    request_id?: string
    history?: { role: string; content: string }[]
    chat_mode?: ChatMode
    history_mode?: boolean
  }): Promise<AxiosResponse> {
    return http.post('/chat/message', payload)
  },
  getHistory(limit = 20): Promise<AxiosResponse> {
    return http.get('/chat/history', { params: { limit } })
  },
  sessions(limit = 50): Promise<AxiosResponse> {
    return http.get('/chat/sessions', { params: { limit } })
  },
  sessionMessages(sessionId: string, limit = 100): Promise<AxiosResponse> {
    return http.get(`/chat/sessions/${sessionId}/messages`, { params: { limit } })
  },
  planning(sessionId: string): Promise<AxiosResponse> {
    return http.get(`/chat/sessions/${sessionId}/planning`)
  },
  deleteSession(sessionId: string): Promise<AxiosResponse> {
    return http.delete(`/chat/sessions/${sessionId}`)
  },
}

// ================================================================
// SSE 通用读取
// ================================================================
interface SSEOptions {
  /** 读到该事件类型即返回其 data（流不在此时关闭，后台继续读完剩余事件） */
  resolveOn?: string
  /** 收到 error 事件时是否 reject */
  throwOnError?: boolean
}

interface Deferred {
  resolve?: (value: any) => void
  reject?: (reason?: any) => void
}

async function consumeSSE(
  resp: Response,
  onEvent: ((event: any) => void) | undefined,
  options: SSEOptions = {},
): Promise<any> {
  const { resolveOn = 'done', throwOnError = false } = options
  const reader = resp.body?.getReader()
  if (!reader) throw new Error('当前浏览器不支持流式响应')

  const decoder = new TextDecoder('utf-8')
  const deferred: Deferred = {}
  let buffer = ''
  let doneData: any = undefined
  let settled = false

  const promise = new Promise<any>((resolve, reject) => {
    deferred.resolve = resolve
    deferred.reject = reject
  })

  const finish = (value: any) => {
    if (settled) return
    settled = true
    deferred.resolve?.(value)
  }

  // 后台持续读取直到流结束（提前 finish 不会中断读取，剩余事件仍会回调）
  void (async () => {
    try {
      for (;;) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })

        let idx = buffer.indexOf('\n\n')
        while (idx >= 0) {
          const raw = buffer.slice(0, idx)
          buffer = buffer.slice(idx + 2)
          idx = buffer.indexOf('\n\n')

          const line = raw.split('\n').find((l) => l.startsWith('data:'))
          if (!line) continue
          let event: any
          try {
            event = JSON.parse(line.slice(5).trim())
          } catch {
            continue
          }
          try {
            onEvent?.(event)
          } catch {
            /* 回调异常不影响流读取 */
          }

          if (event.type === resolveOn) {
            doneData = event.data
            finish(doneData)
          }
          if (event.type === 'error' && throwOnError && !settled) {
            settled = true
            deferred.reject?.(new Error(event.data?.message || '服务端返回错误'))
          }
        }
      }
    } catch (err) {
      if (throwOnError && !settled) {
        settled = true
        deferred.reject?.(err)
      }
    } finally {
      finish(doneData) // 流正常结束：若还没 finish（如未收到 done 事件）则在此结束
    }
  })()

  return promise
}

function authHeaders(): Record<string, string> {
  const token = localStorage.getItem('token')
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

/**
 * 聊天流式输出
 * @returns done 事件的数据（含 reply / used_rag / history_mode / rag_decision / related_guides）
 */
export async function streamChat(
  message: string,
  history: { role: string; content: string }[],
  onEvent?: (event: any) => void,
  options: ChatStreamOptions = {},
): Promise<any> {
  const resp = await fetch('/api/chat/stream', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({
      message,
      history,
      session_id: options.sessionId,
      request_id: options.requestId,
      chat_mode: options.chatMode ?? (options.historyMode ? 'history' : 'normal'),
      // 兼容字段：老后端只认 history_mode
      history_mode: options.chatMode
        ? options.chatMode === 'history'
        : !!options.historyMode,
    }),
  })
  if (!resp.ok) {
    throw new Error(`请求失败（HTTP ${resp.status}）`)
  }
  return consumeSSE(resp, onEvent, { resolveOn: 'done' })
}

/**
 * 攻略总结流式输出
 *
 * 后端把分析做成了**后台任务**：同一个 task_id 只会真正分析一次，
 * 这里每次调用只是"订阅进度"——fromIndex=0 时后端会重放全部历史事件，
 * 因此刷新页面/切走再回来都能追平进度，而不是重新分析。
 *
 * @returns done 事件的数据（最终总结 JSON）
 */
/** 当前用户身份：后端自动把生成的攻略写入 RAG 时用它做归属 */
export interface SummaryOwner {
  user_id?: string
  username?: string
  nickname?: string
}

export async function streamSummary(
  taskId: number,
  onEvent?: (event: any) => void,
  options: { fromIndex?: number; owner?: SummaryOwner } = {},
): Promise<any> {
  const resp = await fetch('/api/summary/stream', {
    method: 'POST',
    headers: authHeaders(),
    body: JSON.stringify({
      task_id: taskId,
      from_index: options.fromIndex ?? 0,
      // 必须带身份：否则后端自动写入 RAG 时会用环境变量默认归属（rag_test_user），
      // 攻略就不属于当前用户，聊天「历史模式」按 user_id 过滤时也检索不到。
      user_id: options.owner?.user_id,
      username: options.owner?.username,
      nickname: options.owner?.nickname,
    }),
  })
  if (!resp.ok) {
    throw new Error(`请求失败（HTTP ${resp.status}）`)
  }
  return consumeSSE(resp, onEvent, { resolveOn: 'done', throwOnError: true })
}

export const summaryApi = {
  /** 任务快照：状态 + 已产生的事件 + 最终总结（刷新后一次性追平） */
  task(taskId: number): Promise<AxiosResponse> {
    return http.get(`/summary/task/${taskId}`)
  },
  /** 取消正在执行的分析 */
  cancel(taskId: number): Promise<AxiosResponse> {
    return http.post(`/summary/cancel/${taskId}`)
  },
}

// ================================================================
// 小红书 MCP：登录状态与实例管理（每用户独立实例）
//
// 注：后端日志已迁到独立服务（log_viewer.py：单独端口 + 固定口令），
//     前端不再提供日志页，因此这里也没有日志相关的 API 封装。
// ================================================================
/** 小红书 MCP：登录状态与实例管理（每用户独立实例） */
export const xhsApi = {
  /** 当前用户的小红书登录/实例状态 */
  status(): Promise<AxiosResponse> {
    return http.get('/xhs/status')
  },
  /** 拉起登录程序（扫码登录自己的小红书），并确保该用户的 MCP 实例已启动 */
  login(): Promise<AxiosResponse> {
    return http.post('/xhs/login')
  },
  startMcp(): Promise<AxiosResponse> {
    return http.post('/xhs/mcp/start')
  },
  stopMcp(): Promise<AxiosResponse> {
    return http.post('/xhs/mcp/stop')
  },
}


// ================================================================
// 意图识别 / 攻略 / 分享 / 用户
// ================================================================
export function recognizeIntent(query: string): Promise<any> {
  return http.post('/intent/recognize', { query }).then((r) => r.data)
}

export const guideApi = {
  save(payload: {
    title: string
    content: any
    destination: string
    days?: number
    summary?: string
  }): Promise<AxiosResponse> {
    return http.post('/guides/save', payload)
  },
  /** 我的攻略（keyword 非空时按标题/目的地/摘要/关键词搜索） */
  myGuides(page = 1, pageSize = 20, keyword = ''): Promise<AxiosResponse> {
    return http.get('/guides/my', {
      params: { page, page_size: pageSize, ...(keyword.trim() ? { keyword: keyword.trim() } : {}) },
    })
  },
  detail(guideId: string): Promise<AxiosResponse> {
    return http.get(`/guides/${guideId}`)
  },
}

export const shareApi = {
  /** 公开攻略（keyword 非空时按标题/目的地/摘要/关键词搜索） */
  publicList(page = 1, pageSize = 20, keyword = ''): Promise<AxiosResponse> {
    return http.get('/share/public', {
      params: { page, page_size: pageSize, ...(keyword.trim() ? { keyword: keyword.trim() } : {}) },
    })
  },
  publicDetail(guideId: string): Promise<AxiosResponse> {
    return http.get(`/share/public/${guideId}`)
  },
  rate(guideId: string, rating: number, ratingText: string): Promise<AxiosResponse> {
    return http.post(`/share/${guideId}/rate`, { rating, rating_text: ratingText })
  },
  publish(guideId: string): Promise<AxiosResponse> {
    return http.post(`/share/${guideId}/publish`)
  },
  unpublish(guideId: string): Promise<AxiosResponse> {
    return http.post(`/share/${guideId}/unpublish`)
  },
}

export const userApi = {
  profile(): Promise<AxiosResponse> {
    return http.get('/user/profile')
  },
  stats(): Promise<AxiosResponse> {
    return http.get('/user/stats')
  },
  guides(page = 1, pageSize = 20, keyword = ''): Promise<AxiosResponse> {
    return http.get('/user/guides', {
      params: { page, page_size: pageSize, ...(keyword.trim() ? { keyword: keyword.trim() } : {}) },
    })
  },
}

export const mcpApi = {
  status(): Promise<AxiosResponse> {
    return http.get('/mcp/status')
  },
}

export default {
  http,
  authApi,
  chatApi,
  xhsApi,
  guideApi,
  shareApi,
  userApi,
  mcpApi,
  apiErrorMessage,
  streamChat,
  streamSummary,
  summaryApi,
  recognizeIntent,
}
