<script setup lang="ts">
/**
 * 旅游分享
 * · 「他人分享」：浏览已公开攻略（支持关键字搜索）
 * · 「我的攻略」：查看/管理自己的攻略（支持关键字搜索），可评价、公开/取消公开
 *   详情与操作统一走 GuideDetailModal（与「我的」页一致）
 */
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { apiErrorMessage, shareApi, guideApi } from '@/lib/api'
import GuideDetailModal from '@/components/GuideDetailModal.vue'

const activeTab = ref<'public' | 'my'>('public')

// 公开攻略列表
const publicGuides = ref<any[]>([])
const publicLoading = ref(false)
const publicPage = ref(1)
const publicTotal = ref(0)
const publicKeyword = ref('')
const publicApplied = ref('')
const PAGE_SIZE = 12

// 我的攻略列表
const myGuides = ref<any[]>([])
const myLoading = ref(false)
const myPage = ref(1)
const myTotal = ref(0)
const myKeyword = ref('')
const myApplied = ref('')

const error = ref('')

// 详情弹窗
const activeGuideId = ref<string | null>(null)
const activeGuidePreview = ref<any>(null)
const activeCanManage = ref(false)

let publicTimer: number | null = null
let myTimer: number | null = null

onMounted(() => {
  loadPublic()
  loadMy()
})

onBeforeUnmount(() => {
  if (publicTimer !== null) window.clearTimeout(publicTimer)
  if (myTimer !== null) window.clearTimeout(myTimer)
})

/** 输入防抖：避免每敲一个字都请求后端 */
watch(publicKeyword, (val) => {
  if (publicTimer !== null) window.clearTimeout(publicTimer)
  publicTimer = window.setTimeout(() => {
    publicTimer = null
    const next = val.trim()
    if (next === publicApplied.value) return
    publicApplied.value = next
    publicPage.value = 1
    loadPublic()
  }, 300)
})

watch(myKeyword, (val) => {
  if (myTimer !== null) window.clearTimeout(myTimer)
  myTimer = window.setTimeout(() => {
    myTimer = null
    const next = val.trim()
    if (next === myApplied.value) return
    myApplied.value = next
    myPage.value = 1
    loadMy()
  }, 300)
})

async function loadPublic() {
  publicLoading.value = true
  error.value = ''
  try {
    const resp = await shareApi.publicList(publicPage.value, PAGE_SIZE, publicApplied.value)
    publicGuides.value = resp.data.items || []
    publicTotal.value = resp.data.total || 0
  } catch (err: any) {
    error.value = apiErrorMessage(err, '加载公开攻略失败')
  } finally {
    publicLoading.value = false
  }
}

async function loadMy() {
  myLoading.value = true
  error.value = ''
  try {
    const resp = await guideApi.myGuides(myPage.value, PAGE_SIZE, myApplied.value)
    myGuides.value = resp.data.items || []
    myTotal.value = resp.data.total || 0
  } catch (err: any) {
    error.value = apiErrorMessage(err, '加载我的攻略失败')
  } finally {
    myLoading.value = false
  }
}

function clearPublicSearch() {
  publicKeyword.value = ''
  publicApplied.value = ''
  publicPage.value = 1
  loadPublic()
}

function clearMySearch() {
  myKeyword.value = ''
  myApplied.value = ''
  myPage.value = 1
  loadMy()
}

function changePublicPage(delta: number) {
  const next = publicPage.value + delta
  if (next < 1 || (next - 1) * PAGE_SIZE >= publicTotal.value) return
  publicPage.value = next
  loadPublic()
}

function changeMyPage(delta: number) {
  const next = myPage.value + delta
  if (next < 1 || (next - 1) * PAGE_SIZE >= myTotal.value) return
  myPage.value = next
  loadMy()
}

function viewGuide(guide: any, canManage: boolean) {
  activeGuidePreview.value = guide
  activeCanManage.value = canManage
  activeGuideId.value = guide.guide_id
}

function closeGuide() {
  activeGuideId.value = null
  activeGuidePreview.value = null
}

async function onGuideUpdated() {
  await Promise.all([loadMy(), loadPublic()])
}

function getContentPreview(content: any): string {
  if (!content) return ''
  if (typeof content === 'string') return content.slice(0, 150)
  return JSON.stringify(content).slice(0, 150)
}
</script>

<template>
  <div>
    <!-- Tab 切换 -->
    <div class="flex gap-2 mb-4">
      <button
        @click="activeTab = 'public'"
        :class="['px-4 py-2 rounded-lg text-sm font-medium transition', activeTab === 'public' ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200']"
      >🌍 他人分享</button>
      <button
        @click="activeTab = 'my'"
        :class="['px-4 py-2 rounded-lg text-sm font-medium transition', activeTab === 'my' ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200']"
      >📋 我的攻略</button>
    </div>

    <!-- 搜索框（两个 Tab 各一个，互不干扰） -->
    <div class="flex flex-wrap items-center gap-2 mb-4">
      <div class="relative">
        <span class="absolute left-3 top-1/2 -translate-y-1/2 text-gray-400 text-sm">🔍</span>
        <input
          v-if="activeTab === 'public'"
          v-model="publicKeyword"
          placeholder="搜索公开攻略：标题 / 目的地 / 摘要"
          class="w-72 pl-9 pr-8 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-400"
        />
        <input
          v-else
          v-model="myKeyword"
          placeholder="搜索我的攻略：标题 / 目的地 / 摘要"
          class="w-72 pl-9 pr-8 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-400"
        />
        <button
          v-if="activeTab === 'public' ? publicKeyword : myKeyword"
          @click="activeTab === 'public' ? clearPublicSearch() : clearMySearch()"
          class="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
          title="清空"
        >&times;</button>
      </div>
      <span v-if="activeTab === 'public'" class="text-xs text-gray-500">
        共 {{ publicTotal }} 条<template v-if="publicApplied">（搜索「{{ publicApplied }}」）</template>
      </span>
      <span v-else class="text-xs text-gray-500">
        共 {{ myTotal }} 条<template v-if="myApplied">（搜索「{{ myApplied }}」）</template>
      </span>
    </div>

    <p v-if="error" class="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">
      {{ error }}
    </p>

    <!-- 公开攻略 -->
    <div v-if="activeTab === 'public'">
      <div v-if="publicLoading" class="text-center text-gray-400 py-10">加载中...</div>
      <div v-else-if="publicGuides.length === 0" class="text-center text-gray-400 py-10">
        <p class="text-3xl mb-2">🌍</p>
        <p>{{ publicApplied ? `没有找到包含「${publicApplied}」的公开攻略` : '暂无公开的旅游攻略' }}</p>
      </div>
      <template v-else>
        <div class="grid gap-4 md:grid-cols-2">
          <div
            v-for="guide in publicGuides"
            :key="guide.guide_id"
            @click="viewGuide(guide, false)"
            class="bg-white rounded-xl shadow-sm border border-gray-100 p-5 hover:shadow-md transition cursor-pointer"
          >
            <div class="flex items-start justify-between mb-2">
              <h3 class="font-bold text-gray-800">{{ guide.title }}</h3>
              <span v-if="guide.rating" class="text-yellow-500 text-sm">⭐ {{ guide.rating }}/5</span>
            </div>
            <div class="flex gap-2 mb-2 text-xs text-gray-500">
              <span>📍 {{ guide.destination }}</span>
              <span v-if="guide.days">📅 {{ guide.days }}天</span>
              <span>👤 {{ guide.nickname || guide.username }}</span>
            </div>
            <p class="text-sm text-gray-600 line-clamp-3">{{ guide.summary || getContentPreview(guide.content) }}</p>
            <div v-if="guide.rating_text" class="mt-2 p-2 bg-yellow-50 rounded text-xs text-yellow-800">
              💬 "{{ guide.rating_text.slice(0, 80) }}"
            </div>
          </div>
        </div>
        <!-- 分页 -->
        <div v-if="publicTotal > PAGE_SIZE" class="flex items-center justify-center gap-3 mt-4 text-sm">
          <button
            @click="changePublicPage(-1)"
            :disabled="publicPage <= 1"
            class="px-3 py-1.5 border border-gray-200 rounded-lg text-gray-600 hover:bg-gray-50 disabled:opacity-40 transition"
          >上一页</button>
          <span class="text-gray-500 text-xs">
            第 {{ publicPage }} / {{ Math.max(1, Math.ceil(publicTotal / PAGE_SIZE)) }} 页
          </span>
          <button
            @click="changePublicPage(1)"
            :disabled="publicPage * PAGE_SIZE >= publicTotal"
            class="px-3 py-1.5 border border-gray-200 rounded-lg text-gray-600 hover:bg-gray-50 disabled:opacity-40 transition"
          >下一页</button>
        </div>
      </template>
    </div>

    <!-- 我的攻略 -->
    <div v-if="activeTab === 'my'">
      <div v-if="myLoading" class="text-center text-gray-400 py-10">加载中...</div>
      <div v-else-if="myGuides.length === 0" class="text-center text-gray-400 py-10">
        <p class="text-3xl mb-2">📋</p>
        <p>{{ myApplied ? `没有找到包含「${myApplied}」的攻略` : '还没有攻略，去生成一个吧！' }}</p>
      </div>
      <template v-else>
        <div class="space-y-3">
          <div
            v-for="guide in myGuides"
            :key="guide.guide_id"
            @click="viewGuide(guide, true)"
            class="bg-white rounded-xl shadow-sm border border-gray-100 p-5 hover:shadow-md transition cursor-pointer"
          >
            <div class="flex items-start justify-between mb-2 gap-3">
              <div class="min-w-0">
                <h3 class="font-bold text-gray-800 truncate">{{ guide.title }}</h3>
                <div class="flex flex-wrap gap-2 mt-1 text-xs text-gray-500">
                  <span>📍 {{ guide.destination }}</span>
                  <span v-if="guide.days">📅 {{ guide.days }}天</span>
                  <span :class="guide.is_public ? 'text-green-600' : 'text-gray-400'">
                    {{ guide.is_public ? '🌐 已公开' : '🔒 未公开' }}
                  </span>
                  <span v-if="guide.rating" class="text-yellow-600">⭐ {{ guide.rating }}/5</span>
                  <span v-else class="text-amber-600">未评价（评价后可公开）</span>
                </div>
              </div>
              <button
                @click.stop="viewGuide(guide, true)"
                class="px-3 py-1 bg-blue-100 text-blue-700 rounded-lg text-xs hover:bg-blue-200 transition shrink-0"
              >
                查看 / 管理
              </button>
            </div>
            <p class="text-sm text-gray-600 line-clamp-2">{{ guide.summary || getContentPreview(guide.content) }}</p>
            <div v-if="guide.rating_text" class="mt-2 text-xs text-gray-500">
              💬 "{{ guide.rating_text.slice(0, 60) }}"
            </div>
          </div>
        </div>
        <!-- 分页 -->
        <div v-if="myTotal > PAGE_SIZE" class="flex items-center justify-center gap-3 mt-4 text-sm">
          <button
            @click="changeMyPage(-1)"
            :disabled="myPage <= 1"
            class="px-3 py-1.5 border border-gray-200 rounded-lg text-gray-600 hover:bg-gray-50 disabled:opacity-40 transition"
          >上一页</button>
          <span class="text-gray-500 text-xs">
            第 {{ myPage }} / {{ Math.max(1, Math.ceil(myTotal / PAGE_SIZE)) }} 页
          </span>
          <button
            @click="changeMyPage(1)"
            :disabled="myPage * PAGE_SIZE >= myTotal"
            class="px-3 py-1.5 border border-gray-200 rounded-lg text-gray-600 hover:bg-gray-50 disabled:opacity-40 transition"
          >下一页</button>
        </div>
      </template>
    </div>

    <!-- 详情弹窗（我的攻略里带评价 / 公开操作） -->
    <GuideDetailModal
      :guide-id="activeGuideId"
      :preview="activeGuidePreview"
      :can-manage="activeCanManage"
      @close="closeGuide"
      @updated="onGuideUpdated"
    />
  </div>
</template>
