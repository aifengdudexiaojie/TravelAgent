<script setup lang="ts">
import { ref, computed } from 'vue'
import type { FinalSummary } from '@/types'

const props = defineProps<{
  summary: FinalSummary
}>()

const emit = defineEmits<{
  restart: []
}>()

// 折叠面板状态
const openSections = ref<Set<string>>(new Set(['daily_plan', 'meta']))

function toggleSection(key: string) {
  const next = new Set(openSections.value)
  next.has(key) ? next.delete(key) : next.add(key)
  openSections.value = next
}

const dailyPlan = computed(() => {
  const plan = props.summary?.daily_plan || {}
  return Object.entries(plan)
})

const meta = computed(() => props.summary?.meta || {})
const spotsCatalog = computed(() => props.summary?.spots_catalog || [])
const foodCatalog = computed(() => props.summary?.food_catalog || [])
const extraRecommendations = computed(() => props.summary?.extra_recommendations || [])
const precautions = computed(() => props.summary?.precautions_summary || {})
const budget = computed(() => props.summary?.budget_breakdown || {})
</script>

<template>
  <div class="space-y-4">
    <!-- ==================== 头部 ==================== -->
    <div class="bg-gradient-to-br from-purple-600 via-blue-600 to-teal-500 p-5 rounded-xl text-white">
      <div class="flex items-center justify-between">
        <h1 class="text-xl font-bold">
          ✈️ {{ meta.destinations?.join(' + ') || '旅行攻略' }}
        </h1>
        <button @click="emit('restart')" class="text-white/80 hover:text-white text-sm bg-white/20 rounded-lg px-3 py-1.5">
          返回重新规划
        </button>
      </div>
      <div class="mt-3 flex flex-wrap gap-1.5 text-xs">
        <span class="bg-white/20 backdrop-blur-sm rounded-full px-3 py-1 font-medium">⏱️ {{ meta.total_days || '天数未知' }}</span>
        <span class="bg-white/20 backdrop-blur-sm rounded-full px-3 py-1 font-medium">📅 {{ meta.date_range?.start || '灵活' }} ~ {{ meta.date_range?.end || '灵活' }}</span>
        <span class="bg-white/20 backdrop-blur-sm rounded-full px-3 py-1 font-medium">💰 预算 {{ meta.budget?.total || '?' }}元</span>
        <span class="bg-white/20 backdrop-blur-sm rounded-full px-3 py-1 font-medium">🏖️ {{ meta.pace }}</span>
        <span v-if="meta.budget?.status" class="bg-white/20 backdrop-blur-sm rounded-full px-3 py-1 font-medium">
          {{ meta.budget.status === 'within_budget' ? '✅ 预算内' : meta.budget.status === 'over_budget' ? '⚠️ 超预算' : '🎯 刚好' }}
        </span>
      </div>
    </div>

    <!-- ==================== ① 每日行程 ==================== -->
    <div class="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
      <button @click="toggleSection('daily_plan')" class="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50">
        <h2 class="font-semibold text-gray-800">🗓️ 每日行程安排</h2>
        <span>{{ openSections.has('daily_plan') ? '▼' : '▶' }}</span>
      </button>

      <div v-if="openSections.has('daily_plan')" class="px-4 pb-4 space-y-4">
        <div v-for="[day, plan] in dailyPlan" :key="day" class="border border-gray-100 rounded-lg overflow-hidden">
          <div class="bg-gray-50 px-3 py-2 flex items-center gap-2">
            <span class="font-medium text-gray-800">{{ day }}</span>
            <span v-if="plan.date" class="text-xs text-gray-500">{{ plan.date }}</span>
            <span v-if="plan.day_theme" class="ml-auto text-xs bg-blue-100 text-blue-700 px-2 py-0.5 rounded-full">{{ plan.day_theme }}</span>
          </div>
          <div class="p-3 space-y-2">
            <div v-for="(period, idx) in plan.periods || []" :key="idx" class="flex gap-3">
              <span class="w-12 shrink-0 text-xs font-medium text-gray-500 pt-1">{{ period.period }}</span>
              <div class="flex-1">
                <p class="text-sm text-gray-800">{{ period.content }}</p>
                <div v-if="period.detail?.name" class="mt-0.5 flex items-center gap-2 text-xs text-gray-500">
                  <span class="font-medium text-gray-600">{{ period.detail.name }}</span>
                  <span v-if="period.detail.duration">⏱️ {{ period.detail.duration }}</span>
                  <span v-if="period.detail.cost">💰 {{ period.detail.cost }}</span>
                  <span v-if="period.detail.coordinates" class="text-gray-400">📍 {{ period.detail.coordinates.lng }}, {{ period.detail.coordinates.lat }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== ② 景点目录 ==================== -->
    <div class="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
      <button @click="toggleSection('spots')" class="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50">
        <h2 class="font-semibold text-gray-800">🏞️ 景点推荐（{{ spotsCatalog.length }}）</h2>
        <span>{{ openSections.has('spots') ? '▼' : '▶' }}</span>
      </button>
      <div v-if="openSections.has('spots')" class="px-4 pb-4 space-y-2">
        <div v-for="(spot, idx) in spotsCatalog" :key="idx" class="border border-gray-100 rounded-lg p-3">
          <div class="flex items-center gap-2">
            <span class="font-medium text-gray-800">{{ spot.name }}</span>
            <span class="text-xs bg-blue-50 text-blue-600 px-2 py-0.5 rounded-full">{{ spot.location }}</span>
            <span v-if="spot.coordinates" class="text-xs text-gray-400">📍 {{ spot.coordinates.lng }}, {{ spot.coordinates.lat }}</span>
          </div>
          <p class="mt-1 text-sm text-gray-600">{{ spot.summary }}</p>
          <div v-if="spot.highlights?.length" class="mt-1 flex flex-wrap gap-1">
            <span v-for="h in spot.highlights" :key="h" class="text-xs bg-amber-50 text-amber-700 px-2 py-0.5 rounded-full">⭐ {{ h }}</span>
          </div>
          <div class="mt-1 flex flex-wrap gap-2 text-xs text-gray-500">
            <span v-if="spot.duration">⏱️ {{ spot.duration }}</span>
            <span v-if="spot.cost">💰 {{ spot.cost }}</span>
            <span v-if="spot.recommendation_count">🔥 {{ spot.recommendation_count }} 人推荐</span>
          </div>
          <div v-if="spot.precautions?.length" class="mt-1">
            <p v-for="p in spot.precautions" :key="p" class="text-xs text-amber-700 bg-amber-50 rounded p-1.5 mt-1">⚠️ {{ p }}</p>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== ③ 美食推荐 ==================== -->
    <div class="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
      <button @click="toggleSection('food')" class="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50">
        <h2 class="font-semibold text-gray-800">🍜 美食推荐（{{ foodCatalog.length }}）</h2>
        <span>{{ openSections.has('food') ? '▼' : '▶' }}</span>
      </button>
      <div v-if="openSections.has('food')" class="px-4 pb-4 space-y-2">
        <div v-for="(food, idx) in foodCatalog" :key="idx" class="border border-gray-100 rounded-lg p-3">
          <div class="flex items-center gap-2">
            <span class="font-medium text-gray-800">{{ food.name }}</span>
            <span v-if="food.location" class="text-xs bg-orange-50 text-orange-600 px-2 py-0.5 rounded-full">{{ food.location }}</span>
          </div>
          <div v-if="food.recommended_dishes?.length" class="mt-1 flex flex-wrap gap-1">
            <span v-for="d in food.recommended_dishes" :key="d" class="text-xs bg-orange-50 text-orange-700 px-2 py-0.5 rounded-full">🍽️ {{ d }}</span>
          </div>
          <div class="mt-1 text-xs text-gray-500">
            <span v-if="food.avg_cost">💰 {{ food.avg_cost }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ==================== ④ 备选推荐 ==================== -->
    <div v-if="extraRecommendations.length" class="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
      <button @click="toggleSection('extra')" class="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50">
        <h2 class="font-semibold text-gray-800">💡 备选推荐（{{ extraRecommendations.length }}）</h2>
        <span>{{ openSections.has('extra') ? '▼' : '▶' }}</span>
      </button>
      <div v-if="openSections.has('extra')" class="px-4 pb-4 space-y-2">
        <div v-for="(item, idx) in extraRecommendations" :key="idx" class="border border-gray-100 rounded-lg p-3">
          <div class="flex items-center gap-2">
            <span class="font-medium text-gray-800">{{ item.name }}</span>
            <span v-if="item.location" class="text-xs bg-gray-100 text-gray-600 px-2 py-0.5 rounded-full">{{ item.location }}</span>
          </div>
          <p class="mt-1 text-sm text-gray-600">{{ item.summary }}</p>
          <p class="mt-1 text-xs text-blue-600">💡 {{ item.recommend_reason }}</p>
        </div>
      </div>
    </div>

    <!-- ==================== ⑤ 避坑汇总 ==================== -->
    <div class="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
      <button @click="toggleSection('precautions')" class="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50">
        <h2 class="font-semibold text-gray-800">⚠️ 避坑提醒</h2>
        <span>{{ openSections.has('precautions') ? '▼' : '▶' }}</span>
      </button>
      <div v-if="openSections.has('precautions')" class="px-4 pb-4 space-y-2">
        <div v-for="(items, category) in precautions" :key="category" class="text-sm">
          <span class="font-medium text-gray-700">{{ category }}：</span>
          <span v-for="item in items" :key="item" class="text-gray-600">{{ item }}；</span>
        </div>
      </div>
    </div>

    <!-- ==================== ⑥ 预算明细 ==================== -->
    <div class="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
      <button @click="toggleSection('budget')" class="w-full flex items-center justify-between px-4 py-3 hover:bg-gray-50">
        <h2 class="font-semibold text-gray-800">💰 预算明细</h2>
        <span>{{ openSections.has('budget') ? '▼' : '▶' }}</span>
      </button>
      <div v-if="openSections.has('budget')" class="px-4 pb-4">
        <div class="grid grid-cols-2 gap-2 text-sm">
          <div v-for="(value, key) in budget" :key="key" class="flex justify-between border-b border-gray-50 py-1">
            <span class="text-gray-500">{{ key }}</span>
            <span class="font-medium text-gray-800">{{ value }}元</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
