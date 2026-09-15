<script setup lang="ts">
/**
 * 用户中心「我的」
 * · 攻略卡片可点击 → 详情弹窗（GuideDetailModal）
 * · 详情里可评价、可公开/取消公开（后端要求先评价才能公开）
 * · 支持按关键字搜索我的攻略（标题/目的地/摘要/关键词，后端过滤并分页）
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { apiErrorMessage, userApi, guideApi } from '@/lib/api'
import GuideDetailModal from '@/components/GuideDetailModal.vue'

const auth = useAuthStore()
const profile = ref<any>(null)
const stats = ref<any>({ total_guides: 0, public_guides: 0, rated_guides: 0 })
const guides = ref<any[]>([])
const loading = ref(true)
const error = ref('')

/** 搜索 */
const keyword = ref('')
const appliedKeyword = ref('')
const searching = ref(false)
let searchTimer: number | null = null

const activeGuideId = ref<string | null>(null)
const activeGuidePreview = ref<any>(null)

const emptyHint = computed(() =>
  appliedKeyword.value
    ? `没有找到包含「${appliedKeyword.value}」的攻略`
    : '还没有生成过攻略',
)

onMounted(load)
onBeforeUnmount(() => {
  if (searchTimer !== null) window.clearTimeout(searchTimer)
})

/** 输入防抖 300ms，避免每敲一个字就打一次后端 */
watch(keyword, (val) => {
  if (searchTimer !== null) window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => {
    searchTimer = null
    const next = val.trim()
    if (next === appliedKeyword.value) return
    appliedKeyword.value = next
    load()
  }, 300)
})

async function load() {
  loading.value = true
  searching.value = !!appliedKeyword.value
  error.value = ''
  try {
    const [profileResp, statsResp, guidesResp] = await Promise.all([
      userApi.profile(),
      userApi.stats(),
      guideApi.myGuides(1, 50, appliedKeyword.value),
    ])
    profile.value = profileResp.data
    stats.value = statsResp.data
    guides.value = guidesResp.data.items || []
  } catch (err: any) {
    error.value = apiErrorMessage(err, '加载失败，请稍后重试')
  } finally {
    loading.value = false
    searching.value = false
  }
}

function clearSearch() {
  keyword.value = ''
  appliedKeyword.value = ''
  load()
}

function openGuide(guide: any) {
  activeGuidePreview.value = guide
  activeGuideId.value = guide.guide_id
}

function closeGuide() {
  activeGuideId.value = null
  activeGuidePreview.value = null
}

async function onGuideUpdated() {
  await load()            // 评价/公开后刷新列表与统计
}
</script>

<template>
  <div>
    <div v-if="loading && !guides.length" class="text-center text-gray-400 py-10">加载中...</div>
    <template v-else>
      <!-- 用户信息卡片 -->
      <div class="bg-white/80 backdrop-blur-sm rounded-xl shadow-lg p-6 mb-6">
        <div class="flex items-center gap-4">
          <div class="w-16 h-16 bg-blue-100 rounded-full flex items-center justify-center text-2xl">
            👤
          </div>
          <div>
            <h2 class="text-xl font-bold text-gray-800">{{ profile?.nickname || profile?.username }}</h2>
            <p class="text-sm text-gray-500">@{{ profile?.username }}</p>
            <p class="text-xs text-gray-400 mt-1">注册于 {{ profile?.created_at?.slice(0, 10) }}</p>
          </div>
        </div>
      </div>

      <!-- 统计 -->
      <div class="grid grid-cols-3 gap-4 mb-6">
        <div class="bg-white rounded-xl shadow-sm border border-gray-100 p-4 text-center">
          <p class="text-2xl font-bold text-blue-600">{{ stats.total_guides }}</p>
          <p class="text-xs text-gray-500 mt-1">总攻略</p>
        </div>
        <div class="bg-white rounded-xl shadow-sm border border-gray-100 p-4 text-center">
          <p class="text-2xl font-bold text-green-600">{{ stats.public_guides }}</p>
          <p class="text-xs text-gray-500 mt-1">已公开</p>
        </div>
        <div class="bg-white rounded-xl shadow-sm border border-gray-100 p-4 text-center">
          <p class="text-2xl font-bold text-yellow-600">{{ stats.rated_guides }}</p>
          <p class="text-xs text-gray-500 mt-1">已评价</p>
        </div>
      </div>

      <!-- 攻略列表 -->
      <div class="bg-white/80 backdrop-blur-sm rounded-xl shadow-lg p-6">
        <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
          <h3 class="text-lg font-bold text-gray-800">📋 我的攻略</h3>
          <!-- 搜索框 -->
          <div class="flex items-center gap-2">
            <div class="relative">
              <span class="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-sm">🔍</span>
              <input
                v-model="keyword"
                placeholder="搜标题 / 目的地 / 摘要"
                class="w-56 pl-9 pr-8 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-400"
              />
              <button
                v-if="keyword"
                @click="clearSearch"
                class="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                title="清空"
              >&times;</button>
            </div>
            <span v-if="searching" class="text-xs text-blue-500">搜索中…</span>
          </div>
        </div>

        <p v-if="appliedKeyword" class="text-xs text-gray-500 mb-3">
          搜索「{{ appliedKeyword }}」：找到 {{ guides.length }} 条
          <button @click="clearSearch" class="ml-2 text-blue-600 hover:underline">清空</button>
        </p>

        <p v-if="error" class="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">
          {{ error }}
        </p>

        <div v-if="guides.length === 0" class="text-center text-gray-400 py-8">
          {{ emptyHint }}
        </div>
        <div v-else class="space-y-3">
          <div
            v-for="guide in guides"
            :key="guide.guide_id"
            @click="openGuide(guide)"
            class="p-4 bg-gray-50 hover:bg-blue-50/60 rounded-lg flex items-center justify-between gap-3 cursor-pointer transition border border-transparent hover:border-blue-100"
            title="点击查看详情"
          >
            <div class="min-w-0">
              <h4 class="font-medium text-gray-800 truncate">{{ guide.title }}</h4>
              <p v-if="guide.summary" class="text-xs text-gray-500 mt-0.5 line-clamp-1">{{ guide.summary }}</p>
              <div class="flex flex-wrap gap-2 mt-1 text-xs text-gray-500">
                <span>📍 {{ guide.destination }}</span>
                <span v-if="guide.days">{{ guide.days }}天</span>
                <span v-if="guide.rating" class="text-yellow-600">⭐ {{ guide.rating }}</span>
                <span :class="guide.is_public ? 'text-green-600' : 'text-gray-400'">
                  {{ guide.is_public ? '🌐 公开' : '🔒 私有' }}
                </span>
                <span v-if="!guide.rating" class="text-amber-600">未评价（评价后可公开）</span>
              </div>
            </div>
            <div class="flex gap-2 shrink-0">
              <button
                @click.stop="openGuide(guide)"
                class="px-3 py-1 bg-white border border-gray-200 rounded text-xs hover:bg-gray-100 transition"
              >查看详情</button>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- 详情弹窗（内含评价与公开开关） -->
    <GuideDetailModal
      :guide-id="activeGuideId"
      :preview="activeGuidePreview"
      @close="closeGuide"
      @updated="onGuideUpdated"
    />
  </div>
</template>
