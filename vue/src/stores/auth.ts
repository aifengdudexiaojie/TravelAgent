import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authApi } from '@/lib/api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(localStorage.getItem('token'))
  let savedUser: any = null
  try {
    savedUser = JSON.parse(localStorage.getItem('user') || 'null')
  } catch {
    localStorage.removeItem('user')
  }
  const user = ref<any>(savedUser)

  const isLoggedIn = computed(() => !!token.value)

  async function login(username: string, password: string) {
    const resp = await authApi.login(username, password)
    const data = resp.data
    token.value = data.access_token
    user.value = data.user
    localStorage.setItem('token', data.access_token)
    localStorage.setItem('user', JSON.stringify(data.user))
  }

  async function register(username: string, password: string, nickname?: string) {
    const resp = await authApi.register(username, password, nickname)
    const data = resp.data
    token.value = data.access_token
    user.value = data.user
    localStorage.setItem('token', data.access_token)
    localStorage.setItem('user', JSON.stringify(data.user))
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem('token')
    localStorage.removeItem('user')
  }

  return { token, user, isLoggedIn, login, register, logout }
})
