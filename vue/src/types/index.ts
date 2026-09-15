// ============================================================
// 阶段1：意图识别
// ============================================================

/** 意图识别返回的结构化内容 */
export interface IntentContent {
  locations: string[]
  days: string | null
  start_date: string | null
  end_date: string | null
  people_count: number | null
  pace: string
  budget_level: string
  budget_amount: number | null
  budget_amount_per_person: number | null
  others: string | null
}

/** 意图识别响应（后端返回 task_id + intent_content） */
export interface RecognizeIntentResponse {
  task_id: number
  intent_content: IntentContent
}

// ============================================================
// 阶段2：SSE 分析进度事件
// ============================================================

/** SSE 进度事件 */
export interface ProgressEvent {
  type: string
  data: any
}

// ============================================================
// 阶段2：最终总结结果
// ============================================================

/** 最终旅游攻略（travel-summarizer.md 输出） */
export interface FinalSummary {
  meta?: {
    destinations?: string[]
    total_days?: string
    date_range?: { start: string | null; end: string | null }
    budget?: { total: number; estimated: number; status: string }
    pace?: string
    generated_at?: string
  }
  trade_off_summary?: {
    total_spots_found?: number
    spots_selected?: number
    spots_excluded?: number
    exclusion_reasons?: Record<string, string>
  }
  daily_plan?: Record<string, any>
  spots_catalog?: any[]
  food_catalog?: any[]
  extra_recommendations?: any[]
  precautions_summary?: Record<string, string[]>
  budget_breakdown?: Record<string, number>
  [key: string]: any
}
