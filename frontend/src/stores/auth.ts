import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { backend } from '@/api/client'
import type { CurrentUser } from '@/api/types'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<CurrentUser | null>(null)
  const loaded = ref(false)
  const isAdmin = computed(() => user.value?.role === 'ADMIN')

  async function loadMe() {
    try {
      user.value = await backend.me()
    } finally {
      loaded.value = true
    }
  }

  async function login(username: string, password: string) {
    await backend.login(username, password)
    user.value = await backend.me()
    loaded.value = true
  }

  async function logout() {
    await backend.logout()
    user.value = null
    loaded.value = true
  }

  return { user, loaded, isAdmin, loadMe, login, logout }
})
