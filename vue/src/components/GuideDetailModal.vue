<script setup lang="ts">
/**
 * 攻略详情弹窗（「我的」与「旅游分享」共用）
 *
 * · 打开时按 guide_id 拉完整详情（GET /api/guides/{id}）
 * · 本人攻略（is_owner）才显示管理操作：评价（⭐1-5 + 文字）、公开 / 取消公开
 * · 内容优先用 FinalSummary 渲染（与生成攻略页一致）；结构不认识时回退为原文
 *
 * 两个后端约束必须在界面上说清楚（以前只在 placeholder 里提了一句，用户看不到）：
 *   1. 评价内容 **10–500 字**（models/schemas.py 的 GuideRate）→ 实时字数计数器 + 还差几字
 *   2. **必须先评价才能公开**（share_routes.publish 会校验 rating）→ 未评价时公开按钮禁用并说明原因
 */
import { computed, ref, watch } from 'vue'
import { apiErrorMessage, guideApi, shareApi } from '@/lib/api'
import FinalSummary from '@/components/FinalSummary.vue'

const MIN_RATING_CHARS = 10
const MAX_RATING_CHARS = 500

const props = withDefaults(
  defineProps<{
    /** 非空即显示弹窗 */
    guideId: string | null
    /** 列表里已有的信息：先渲染出来，避免打开瞬间白屏 */
    preview?: any
    /** 是否显示管理操作；不传则用详情里的 is_owner */
    canManage?: boolean
  }>(),
  { preview: null, canManage: undefined },
)

const emit = defineEmits<{
  close: []
  updated: [guide: any]
}>()

const detail = ref<any>(null)
const loading = ref(false)
const error = ref('')
const actionError = ref('')
const notice = ref('')

const rating = ref(5)
const ratingText = ref('')
const savingRating = ref(false)
const togglingPublish = ref(false)
/** 提交过一次后才实时红字提示，避免刚打开就报错 */
const ratingTouched = ref(false)

const isOwner = computed(() =>
  props.canManage === undefined ? !!detail.value?.is_owner : props.canManage,
)
const isPublic = computed(() => !!detail.value?.is_public)
const hasRating = computed(() => detail.value?.rating != null)

/** 按后端规则统计（去首尾空白） */
const ratingLength = computed(() => ratingText.value.trim().length)
const ratingTooShort = computed(() => ratingLength.value < MIN_RATING_CHARS)
const ratingCountClass = computed(() => {
  if (ratingLength.value === 0) return 'text-gray-400'
  return ratingTooShort.value ? 'text-amber-600' : 'text-green-600'
})
const ratingHint = computed(() => {
  if (ratingLength.value === 0) {
    return `评价内容需 ${MIN_RATING_CHARS}–${MAX_RATING_CHARS} 字，写够才能提交`
  }
  if (ratingTooShort.value) {
    return `还差 ${MIN_RATING_CHARS - ratingLength.value} 个字（至少 ${MIN_RATING_CHARS} 字）`
  }
  return '内容符合要求，可以提交'
})
/** 只在"填了内容但不够字"时红字提示 */
const showRatingWarning = computed(() => ratingLength.value > 0 && ratingTooShort.value)

/** 空内容时按钮不可点（点了也不会有任何请求），其余情况都可点并给出明确原因 */
const submitDisabled = computed(
  () => savingRating.value || ratingLength.value === 0 || !props.guideId,
)

/** content 是结构化总结时交给 FinalSummary 渲染 */
const structuredContent = computed(() => {
  const c = detail.value?.content
  if (c && typeof c === 'object' && Object.keys(c).length > 0) return c
  return null
})
const rawContent = computed(() => {
  const c = detail.value?.content
  if (c == null) return ''
  return typeof c === 'string' ? c : JSON.stringify(c, null, 2)
})

watch(
  () => props.guideId,
  async (id) => {
    actionError.value = ''
    notice.value = ''
    error.value = ''
    ratingTouched.value = false
    if (!id) {
      detail.value = null
      return
    }
    detail.value = props.preview ?? null          // 先用列表数据占位
    rating.value = props.preview?.rating || 5
    ratingText.value = props.preview?.rating_text || ''
    loading.value = true
    try {
      const resp = await guideApi.detail(id)
      detail.value = resp.data
      rating.value = resp.data?.rating || 5
      ratingText.value = resp.data?.rating_text || ''
    } catch (err: any) {
      error.value = apiErrorMessage(err, '加载攻略详情失败')
    } finally {
      loading.value = false
    }
  },
  { immediate: true },
)

async function reload() {
  if (!props.guideId) return
  const resp = await guideApi.detail(props.guideId)
  detail.value = resp.data
  emit('updated', resp.data)
}

async function submitRating() {
  if (!props.guideId || savingRating.value) return
  ratingTouched.value = true
  actionError.value = ''
  notice.value = ''

  // 本地先拦住：把"为什么不能提交"讲清楚，而不是让按钮点了没反应
  if (ratingLength.value < MIN_RATING_CHARS) {
    actionError.value = ratingLength.value === 0
      ? `请先填写评价内容（${MIN_RATING_CHARS}–${MAX_RATING_CHARS} 字）`
      : `评价内容至少 ${MIN_RATING_CHARS} 个字：当前 ${ratingLength.value} 个字，还差 ` +
        `${MIN_RATING_CHARS - ratingLength.value} 个字`
    return
  }
  if (ratingLength.value > MAX_RATING_CHARS) {
    actionError.value = `评价内容最多 ${MAX_RATING_CHARS} 个字，请精简 ${ratingLength.value - MAX_RATING_CHARS} 个字`
    return
  }

  savingRating.value = true
  try {
    await shareApi.rate(props.guideId, rating.value, ratingText.value.trim())
    await reload()
    notice.value = '评价已保存，现在可以公开分享这篇攻略了'
    actionError.value = ''
  } catch (err: any) {
    actionError.value = apiErrorMessage(err, '评价失败，请稍后重试')
  } finally {
    savingRating.value = false
  }
}

async function togglePublish() {
  if (!props.guideId) return
  togglingPublish.value = true
  actionError.value = ''
  notice.value = ''
  try {
    if (isPublic.value) {
      await shareApi.unpublish(props.guideId)
      notice.value = '已取消公开，其他人在分享页看不到它了'
    } else {
      if (!hasRating.value) {
        // 双保险：后端也会拦，但这里直接说明原因
        actionError.value = '请先在上方完成评价（10 字以上）后再公开'
        togglingPublish.value = false
        return
      }
      await shareApi.publish(props.guideId)
      notice.value = '已公开：现在会出现在「旅游分享」里'
    }
    await reload()
  } catch (err: any) {
    actionError.value = apiErrorMessage(err, '操作失败，请稍后重试')
  } finally {
    togglingPublish.value = false
  }
}
</script>

<template>
  <div
    v-if="guideId"
    class="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4"
    @click.self="emit('close')"
  >
    <div class="bg-white rounded-2xl w-full max-w-3xl max-h-[86vh] flex flex-col overflow-hidden">
      <!-- 头部 -->
      <div class="flex items-start justify-between gap-3 p-5 border-b border-gray-100">
        <div class="min-w-0">
          <h3 class="text-lg font-bold text-gray-800 truncate">
            {{ detail?.title || '攻略详情' }}
          </h3>
          <div class="flex flex-wrap items-center gap-2 mt-1.5 text-xs text-gray-500">
            <span>📍 {{ detail?.destination || '—' }}</span>
            <span v-if="detail?.days">📅 {{ detail.days }}天</span>
            <span v-if="detail?.username">👤 {{ detail?.nickname || detail?.username }}</span>
            <span v-if="hasRating" class="text-yellow-600">⭐ {{ detail.rating }}/5</span>
            <span
              class="px-2 py-0.5 rounded-full"
              :class="isPublic ? 'bg-green-50 text-green-600' : 'bg-gray-100 text-gray-500'"
            >
              {{ isPublic ? '🌐 已公开' : '🔒 私有' }}
            </span>
            <span v-if="detail?.is_owner" class="text-blue-500">（我的攻略）</span>
          </div>
        </div>
        <button
          @click="emit('close')"
          class="text-gray-400 hover:text-gray-600 text-2xl leading-none shrink-0"
        >&times;</button>
      </div>

      <!-- 管理区：仅本人攻略 -->
      <div v-if="isOwner" class="px-5 py-4 bg-gray-50 border-b border-gray-100 space-y-3">
        <!-- 评价 -->
        <div>
          <div class="flex flex-wrap items-center gap-3">
            <span class="text-xs text-gray-500 shrink-0">评价</span>
            <div class="flex gap-1">
              <button
                v-for="s in 5"
                :key="s"
                @click="rating = s"
                :title="`打 ${s} 分`"
                :class="['w-7 h-7 rounded text-sm transition', s <= rating ? 'bg-yellow-400 text-white' : 'bg-white border border-gray-200 text-gray-300']"
              >⭐</button>
            </div>
            <span class="text-xs text-gray-500">{{ rating }} 分</span>
          </div>

          <textarea
            v-model="ratingText"
            :maxlength="MAX_RATING_CHARS"
            rows="2"
            placeholder="写点评价，例如：行程节奏合适，景点安排不赶，适合带家人"
            class="w-full mt-2 px-3 py-2 text-xs border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-400 resize-none"
            :class="showRatingWarning ? 'border-amber-300 bg-amber-50/40' : 'border-gray-200'"
          ></textarea>

          <!-- 字数要求：始终可见 -->
          <div class="flex flex-wrap items-center justify-between gap-2 mt-1">
            <span class="text-xs" :class="showRatingWarning ? 'text-amber-600' : 'text-gray-500'">
              {{ ratingHint }}
            </span>
            <span class="text-xs tabular-nums" :class="ratingCountClass">
              {{ ratingLength }} / {{ MAX_RATING_CHARS }} 字
            </span>
          </div>

          <div class="flex justify-end mt-2">
            <button
              @click="submitRating"
              :disabled="submitDisabled"
              :title="submitDisabled ? '请先填写评价内容' : '提交评价（10 字以上）'"
              class="px-4 py-1.5 text-xs rounded-lg bg-blue-600 text-white hover:bg-blue-700 transition disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {{ savingRating ? '保存中…' : (hasRating ? '更新评价' : '提交评价') }}
            </button>
          </div>
        </div>

        <!-- 公开开关 -->
        <div class="flex flex-wrap items-center gap-3 pt-2 border-t border-gray-200">
          <span class="text-xs text-gray-500 shrink-0">公开</span>
          <button
            @click="togglePublish"
            :disabled="togglingPublish || (!isPublic && !hasRating)"
            :title="!isPublic && !hasRating ? '需先完成评价才能公开' : ''"
            :class="[
              'px-4 py-1.5 text-xs rounded-lg transition disabled:opacity-50 disabled:cursor-not-allowed',
              isPublic ? 'bg-gray-100 text-gray-600 hover:bg-gray-200' : 'bg-green-600 text-white hover:bg-green-700',
            ]"
          >
            {{ togglingPublish ? '处理中…' : (isPublic ? '取消公开' : '公开分享') }}
          </button>
          <span v-if="!isPublic && !hasRating" class="text-xs text-amber-600">
            ⚠️ 需先在上方评价（10 字以上），之后才能公开
          </span>
          <span v-else-if="!isPublic" class="text-xs text-gray-400">
            公开后其他用户能在「旅游分享」里看到并评价
          </span>
          <span v-else class="text-xs text-green-600">🌐 其他人已可以看到这篇攻略</span>
        </div>

        <p v-if="actionError" class="text-xs text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2">
          {{ actionError }}
        </p>
        <p v-if="notice" class="text-xs text-green-700 bg-green-50 border border-green-100 rounded-lg px-3 py-2">
          {{ notice }}
        </p>
      </div>

      <!-- 他人评价 -->
      <div v-if="!isOwner && detail?.rating_text" class="px-5 pt-4">
        <p class="text-sm text-yellow-800 bg-yellow-50 border border-yellow-100 rounded-lg p-3">
          ⭐ {{ detail.rating }}/5 · {{ detail.rating_text }}
        </p>
      </div>

      <!-- 内容 -->
      <div class="flex-1 overflow-y-auto p-5">
        <p v-if="loading" class="text-center text-gray-400 py-8 text-sm">加载中…</p>
        <p v-else-if="error" class="text-center text-red-500 py-8 text-sm">{{ error }}</p>
        <FinalSummary v-else-if="structuredContent" :summary="structuredContent" />
        <pre v-else-if="rawContent" class="whitespace-pre-wrap text-sm text-gray-700 bg-gray-50 p-4 rounded-lg">{{ rawContent }}</pre>
        <p v-else class="text-center text-gray-400 py-8 text-sm">这篇攻略没有可展示的内容</p>
      </div>
    </div>
  </div>
</template>
