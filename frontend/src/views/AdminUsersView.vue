<template>
  <div class="page" v-loading="loading">
    <ErrorState :message="error" />
    <section class="panel">
      <div class="toolbar">
        <el-input v-model="form.username" placeholder="用户名" style="width: 160px" />
        <el-input v-model="form.password" placeholder="初始密码" type="password" style="width: 180px" />
        <el-select v-model="form.role" style="width: 130px">
          <el-option label="VIEWER" value="VIEWER" />
          <el-option label="ADMIN" value="ADMIN" />
        </el-select>
        <el-button type="primary" @click="create">创建用户</el-button>
      </div>
    </section>
    <section class="panel">
      <h2 class="panel-title">用户管理</h2>
      <el-table :data="rows">
        <el-table-column prop="username" label="用户名" />
        <el-table-column prop="display_name" label="显示名" />
        <el-table-column prop="role" label="角色" />
        <el-table-column prop="is_active" label="启用" />
        <el-table-column prop="created_at" label="创建时间" />
      </el-table>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import ErrorState from '@/components/ErrorState.vue'
import { ApiClientError, backend } from '@/api/client'

const rows = ref<Array<Record<string, unknown>>>([])
const loading = ref(false)
const error = ref('')
const form = reactive({ username: '', password: '', role: 'VIEWER' })

async function load() {
  loading.value = true
  error.value = ''
  try {
    rows.value = (await backend.users()).items
  } catch (exc) {
    error.value = (exc as ApiClientError).message
  } finally {
    loading.value = false
  }
}

async function create() {
  try {
    await backend.createUser({ ...form })
    form.username = ''
    form.password = ''
    await load()
  } catch (exc) {
    error.value = (exc as ApiClientError).message
  }
}

onMounted(load)
</script>
