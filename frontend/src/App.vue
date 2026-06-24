<template>
  <router-view v-if="$route.meta.public" />
  <el-container v-else class="app-shell">
    <el-aside width="216px" class="sidebar">
      <div class="brand">Stock Guard</div>
      <el-menu router :default-active="$route.path" class="menu">
        <el-menu-item index="/">首页</el-menu-item>
        <el-menu-item index="/signals">选股信号</el-menu-item>
        <el-menu-item index="/market">市场数据</el-menu-item>
        <el-menu-item index="/stocks">股票详情</el-menu-item>
        <el-menu-item index="/backtests">回测结果</el-menu-item>
        <el-menu-item index="/paper">模拟账户</el-menu-item>
        <el-menu-item index="/system">系统状态</el-menu-item>
        <el-menu-item v-if="auth.isAdmin" index="/admin/users">用户管理</el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="topbar">
        <div class="top-title">Stock Guard</div>
        <el-tag type="warning" effect="dark">PAPER_TRADING</el-tag>
        <el-tag>{{ summary?.data_mode || 'UNKNOWN' }}</el-tag>
        <el-tag :type="summary?.provider_status === 'OK' ? 'success' : 'info'">{{ summary?.provider_status || 'NOT_CONFIGURED' }}</el-tag>
        <span class="spacer"></span>
        <el-dropdown v-if="auth.user">
          <span class="user-menu">{{ auth.user.username }} / {{ auth.user.role }}</span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item @click="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        <span class="clock">{{ nowText }}</span>
        <el-button :icon="Refresh" @click="refresh" :loading="loading">刷新</el-button>
      </el-header>
      <el-main class="main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { backend } from './api/client'
import type { DashboardSummary } from './api/types'
import { useAuthStore } from './stores/auth'

const summary = ref<DashboardSummary | null>(null)
const loading = ref(false)
const nowText = ref(new Date().toLocaleString('zh-CN', { hour12: false }))
const auth = useAuthStore()
const router = useRouter()
let timer = 0

async function refresh() {
  loading.value = true
  try {
    summary.value = await backend.dashboard()
  } finally {
    loading.value = false
  }
}

async function logout() {
  await auth.logout()
  router.push('/login')
}

onMounted(() => {
  refresh()
  timer = window.setInterval(() => {
    nowText.value = new Date().toLocaleString('zh-CN', { hour12: false })
  }, 1000)
})

onUnmounted(() => window.clearInterval(timer))
</script>
