<template>
  <div class="login-page">
    <section class="login-panel">
      <h1>Stock Guard</h1>
      <el-tag type="warning" effect="dark">PAPER_TRADING</el-tag>
      <el-form class="login-form" @submit.prevent="submit">
        <el-form-item>
          <el-input v-model="username" placeholder="用户名" autocomplete="username" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" placeholder="密码" type="password" autocomplete="current-password" show-password />
        </el-form-item>
        <ErrorState :message="error" />
        <el-button type="primary" native-type="submit" :loading="loading" class="login-button">登录</el-button>
      </el-form>
    </section>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import ErrorState from '@/components/ErrorState.vue'
import { ApiClientError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

async function submit() {
  loading.value = true
  error.value = ''
  try {
    await auth.login(username.value, password.value)
    router.push(String(route.query.redirect || '/'))
  } catch (exc) {
    const err = exc as ApiClientError
    error.value = err.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  background: #f3f6fb;
}

.login-panel {
  width: 360px;
  background: #fff;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 28px;
}

.login-panel h1 {
  margin: 0 0 12px;
}

.login-form {
  margin-top: 22px;
}

.login-button {
  width: 100%;
}
</style>
