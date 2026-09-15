<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const auth = useAuthStore()

const isLogin = ref(true)
const username = ref('')
const password = ref('')
const nickname = ref('')
const loading = ref(false)
const error = ref('')

async function handleSubmit() {
  username.value = username.value.trim()
  nickname.value = nickname.value.trim()

  if (!username.value) {
    error.value = '请填写用户名'
    return
  }
  if (username.value.length < 3) {
    error.value = '用户名至少3个字符'
    return
  }
  if (!password.value) {
    error.value = '请填写密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    if (isLogin.value) {
      await auth.login(username.value, password.value)
    } else {
      if (password.value.length < 6) {
        error.value = '密码至少6位'
        loading.value = false
        return
      }
      await auth.register(username.value, password.value, nickname.value || undefined)
    }
    router.push('/')
  } catch (err: any) {
    const detail = err.response?.data?.detail
    error.value = Array.isArray(detail)
      ? (detail[0]?.msg || '操作失败，请重试')
      : (detail || '操作失败，请重试')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="min-h-screen bg-gradient-to-br from-blue-50 via-white to-purple-50 flex items-center justify-center">
    <div class="w-full max-w-md p-8">
      <div class="text-center mb-8">
        <h1 class="text-3xl font-bold text-gray-900">🤖 AI 旅行规划助手</h1>
        <p class="text-gray-500 mt-2">智能攻略 · RAG 检索 · 小红书数据</p>
      </div>

      <div class="bg-white/80 backdrop-blur-sm rounded-2xl shadow-lg p-8">
        <div class="flex mb-6 bg-gray-100 rounded-lg p-1">
          <button
            @click="isLogin = true"
            :class="['flex-1 py-2 rounded-md text-sm font-medium transition', isLogin ? 'bg-white shadow text-blue-600' : 'text-gray-500']"
          >登录</button>
          <button
            @click="isLogin = false"
            :class="['flex-1 py-2 rounded-md text-sm font-medium transition', !isLogin ? 'bg-white shadow text-blue-600' : 'text-gray-500']"
          >注册</button>
        </div>

        <div v-if="error" class="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-red-600 text-sm">
          {{ error }}
        </div>

        <form @submit.prevent="handleSubmit" class="space-y-4">
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">用户名</label>
            <input
              v-model="username"
              type="text"
              placeholder="请输入用户名"
              class="w-full px-4 py-3 border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none transition"
            />
          </div>
          <div>
            <label class="block text-sm font-medium text-gray-700 mb-1">密码</label>
            <input
              v-model="password"
              type="password"
              placeholder="请输入密码"
              class="w-full px-4 py-3 border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none transition"
            />
          </div>
          <div v-if="!isLogin">
            <label class="block text-sm font-medium text-gray-700 mb-1">昵称（可选）</label>
            <input
              v-model="nickname"
              type="text"
              placeholder="给自己取个昵称"
              class="w-full px-4 py-3 border border-gray-200 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-transparent outline-none transition"
            />
          </div>
          <button
            type="submit"
            :disabled="loading"
            class="w-full py-3 bg-blue-600 hover:bg-blue-700 text-white font-medium rounded-lg transition disabled:opacity-50"
          >
            {{ loading ? '处理中...' : (isLogin ? '登录' : '注册') }}
          </button>
        </form>
      </div>
    </div>
  </div>
</template>
