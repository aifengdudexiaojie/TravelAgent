<script setup lang="ts">
/**
 * 分析进度时间线
 *  · 顶部进度条（已分析地点 / 总地点）
 *  · 只渲染最近 N 条事件（长任务事件很多，避免 DOM 爆炸）
 *  · 新事件到达自动滚到底部
 */
import { computed, nextTick, ref, watch } from 'vue'
import type { ProgressEvent } from '@/types'

const props = defineProps<{
  events: ProgressEvent[]
  isAnalyzing: boolean
}>()

/** 时间线最多渲染多少条（保留最近的） */
const MAX_RENDER = 150

const listEl = ref<HTMLElement | null>(null)

const addressCount = computed(() => props.events.filter(e => e.type === 'address_done').length)
const addressTotal = computed(
  () => [...props.events].reverse().find(e => e.data?.total)?.data?.total || 0,
)
const percent = computed(() =>
  addressTotal.value ? Math.min(100, Math.round((addressCount.value / addressTotal.value) * 100)) : 0,
)

const currentAddress = computed(() => {
  const last = [...props.events].reverse().find(e => e.type === 'address_start')
  return last?.data?.location || ''
})

const currentPost = computed(() => {
  const last = [...props.events].reverse().find(e => e.type === 'post_start')
  return last?.data?.index ? `${last.data.index}/${last.data.total}` : ''
})

/** 展示用事件（截断 + 文案） */
const displayEvents = computed(() =>
  props.events.slice(-MAX_RENDER).map(e => {
    switch (e.type) {
      case 'start': return { icon: '🚀', text: '开始分析' }
      case 'address_start': return { icon: '📍', text: `开始分析：${e.data?.location || ''}` }
      case 'post_start': return { icon: '📄', text: `分析帖子 ${e.data?.index ?? ''}/${e.data?.total ?? ''}` }
      case 'address_done': return { icon: '✅', text: `完成：${e.data?.location || ''}` }
      case 'saving': return { icon: '📚', text: e.data?.message || '正在写入知识库…' }
      case 'saved': return { icon: '🎉', text: '已写入知识库' }
      case 'validation_error': return { icon: '⚠️', text: `未写入知识库：${e.data?.message || ''}` }
      case 'save_error': return { icon: '⚠️', text: `入库失败：${e.data?.message || ''}` }
      case 'done': return { icon: '🎉', text: '全部完成' }
      case 'error': return { icon: '❌', text: `出错：${e.data?.message || ''}` }
      default: return { icon: '•', text: String(e.type) }
    }
  }),
)

watch(
  () => props.events.length,
  async () => {
    await nextTick()
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight
  },
)
</script>

<template>
  <div class="h-full flex flex-col">
    <!-- 头部：进度条 + 计数 -->
    <div class="pb-3">
      <div class="flex gap-2 text-xs mb-2">
        <span class="bg-blue-50 text-blue-700 px-2 py-0.5 rounded-full font-medium">
          📍 已完成地点: {{ addressCount }}<template v-if="addressTotal"> / {{ addressTotal }}</template>
        </span>
        <span v-if="currentAddress" class="bg-indigo-50 text-indigo-700 px-2 py-0.5 rounded-full font-medium">
          🧭 当前: {{ currentAddress }}
        </span>
        <span v-if="currentPost" class="bg-purple-50 text-purple-700 px-2 py-0.5 rounded-full font-medium">
          📄 当前帖子: {{ currentPost }}
        </span>
      </div>

      <div class="h-1.5 w-full bg-gray-100 rounded-full overflow-hidden">
        <div
          class="h-full bg-gradient-to-r from-blue-500 to-purple-500 transition-all duration-500"
          :class="{ 'animate-pulse': isAnalyzing && !percent }"
          :style="{ width: (percent || (isAnalyzing ? 8 : 0)) + '%' }"
        ></div>
      </div>
    </div>

    <!-- 事件时间线 -->
    <div ref="listEl" class="flex-1 overflow-auto max-h-[calc(100vh-380px)] pr-1">
      <div v-if="events.length === 0" class="flex flex-col items-center justify-center h-48 text-gray-500">
        <div class="relative w-14 h-14 mb-3">
          <span class="absolute inset-0 rounded-full border-4 border-blue-100"></span>
          <span class="absolute inset-0 rounded-full border-4 border-blue-500 border-t-transparent animate-spin"></span>
        </div>
        <p class="text-base font-medium">正在准备分析…</p>
        <p class="text-xs text-gray-400 mt-1">正在检索小红书帖子并整理行程</p>
      </div>

      <div v-else class="relative pl-6">
        <div class="absolute left-2 top-0 bottom-0 w-0.5 bg-gradient-to-b from-blue-200 via-purple-200 to-green-200"></div>

        <div
          v-for="(event, index) in displayEvents"
          :key="index"
          class="relative pb-3"
        >
          <div class="absolute left-[-1.15rem] top-1 w-2.5 h-2.5 rounded-full border-2 border-white bg-gradient-to-r from-blue-300 to-purple-300 shadow-sm"></div>
          <div class="bg-white rounded-lg shadow-sm border border-gray-100 p-2.5 text-sm text-gray-700">
            {{ event.icon }} {{ event.text }}
          </div>
        </div>

        <!-- 尾部：分析中动画 -->
        <div v-if="isAnalyzing" class="relative pb-1 flex items-center gap-2 text-xs text-blue-600">
          <span class="inline-block w-3 h-3 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"></span>
          分析进行中…
        </div>
      </div>
    </div>
  </div>
</template>
