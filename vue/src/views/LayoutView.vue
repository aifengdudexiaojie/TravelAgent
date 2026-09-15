<script setup lang="ts">
import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { usePlanStore } from '@/stores/plan'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()
const plan = usePlanStore()

const tabs = [
  { path: '/chat', title: '聊天', icon: '💬' },
  { path: '/guide', title: '旅游攻略', icon: '🗺️' },
  { path: '/share', title: '旅游分享', icon: '🌍' },
  { path: '/profile', title: '我的', icon: '👤' },
  { path: '/logs', title: '后端日志', icon: '🖥️' },
]

const activeTab = computed(() => route.path)
/** 攻略分析进行中：在其他 Tab 上也能看到"还在跑" */
const analyzing = computed(() => plan.stage === 'analyzing')

function handleLogout() {
  auth.logout()
  router.push('/login')
}
</script>

<template>
  <div class="min-h-screen bg-gradient-to-br from-blue-50 via-white to-purple-50 flex flex-col">
    <!-- 顶部导航栏 -->
    <header class="bg-white/80 backdrop-blur-sm border-b border-gray-100 sticky top-0 z-50">
      <div class="container mx-auto px-4 max-w-6xl">
        <div class="flex items-center justify-between h-14">
          <h1 class="text-lg font-bold text-gray-900">🤖 AI 旅行规划</h1>
          <div class="flex items-center gap-3">
            <!-- 切到别的 Tab 时，攻略分析仍在后台跑 -->
            <router-link
              v-if="analyzing && activeTab !== '/guide'"
              to="/guide"
              class="flex items-center gap-1.5 text-xs px-2.5 py-1 rounded-full bg-blue-50 border border-blue-200 text-blue-700 hover:bg-blue-100 transition"
              :title="plan.progressText"
            >
              <span class="inline-block w-3 h-3 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"></span>
              {{ plan.progressText }}
            </router-link>
            <span class="text-sm text-gray-500">{{ auth.user?.nickname || auth.user?.username }}</span>
            <button @click="handleLogout" class="text-sm text-gray-400 hover:text-red-500 transition">退出</button>
          </div>
        </div>
        <!-- TabBar -->
        <nav class="flex -mb-px overflow-x-auto">
          <router-link
            v-for="tab in tabs"
            :key="tab.path"
            :to="tab.path"
            :class="[
              'flex items-center gap-1.5 px-4 py-3 text-sm font-medium border-b-2 transition whitespace-nowrap',
              activeTab === tab.path
                ? 'border-blue-600 text-blue-600'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
            ]"
          >
            <span>{{ tab.icon }}</span>
            <span>{{ tab.title }}</span>
            <span
              v-if="tab.path === '/guide' && analyzing"
              class="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse"
            ></span>
          </router-link>
        </nav>
      </div>
    </header>

    <!-- 主内容区 -->
    <main class="flex-1 container mx-auto px-4 py-6 max-w-6xl">
      <router-view />
    </main>
  </div>
</template>
