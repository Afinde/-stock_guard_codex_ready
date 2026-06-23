<template>
  <div class="page" v-loading="loading">
    <ErrorState :message="error" :request-id="requestId" />
    <section class="panel">
      <h2 class="panel-title">系统状态</h2>
      <el-descriptions :column="3" border>
        <el-descriptions-item label="环境">{{ status.environment }}</el-descriptions-item>
        <el-descriptions-item label="部署">{{ status.deployment_profile }}</el-descriptions-item>
        <el-descriptions-item label="数据模式">{{ status.data_mode }}</el-descriptions-item>
        <el-descriptions-item label="Provider">{{ status.provider_status }}</el-descriptions-item>
        <el-descriptions-item label="准入">{{ status.admission_status }}</el-descriptions-item>
        <el-descriptions-item label="迁移">{{ status.migration_required ? 'MIGRATION_REQUIRED' : 'OK' }}</el-descriptions-item>
        <el-descriptions-item label="当前Revision">{{ status.current_revision }}</el-descriptions-item>
        <el-descriptions-item label="Head">{{ status.head_revision }}</el-descriptions-item>
        <el-descriptions-item label="数据库大小">{{ status.database_size }}</el-descriptions-item>
      </el-descriptions>
    </section>
    <section class="panel">
      <h2 class="panel-title">能力开关</h2>
      <el-space wrap>
        <el-tag v-for="(enabled, key) in status.capabilities || {}" :key="key" :type="enabled ? 'success' : 'info'">{{ key }}: {{ enabled ? '启用' : '未启用' }}</el-tag>
      </el-space>
      <div class="toolbar action-row">
        <el-button type="primary" :disabled="!status.capabilities?.light_scan" @click="createScan">创建轻量扫描任务</el-button>
      </div>
    </section>
    <section class="panel">
      <h2 class="panel-title">最近任务</h2>
      <el-table :data="jobs">
        <el-table-column prop="job_id" label="任务" min-width="180" />
        <el-table-column prop="task_type" label="类型" />
        <el-table-column prop="status" label="状态" />
        <el-table-column prop="started_at" label="开始" />
        <el-table-column prop="completed_at" label="完成" />
      </el-table>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ErrorState from '@/components/ErrorState.vue'
import { ApiClientError, backend } from '@/api/client'
import type { Job } from '@/api/types'

const loading = ref(false)
const error = ref('')
const requestId = ref('')
const status = ref<Record<string, any>>({})
const jobs = ref<Job[]>([])

async function load() {
  loading.value = true
  error.value = ''
  try {
    status.value = await backend.system()
    jobs.value = (await backend.jobs()).items
  } catch (exc) {
    const err = exc as ApiClientError
    error.value = err.message
    requestId.value = err.requestId
  } finally {
    loading.value = false
  }
}

async function createScan() {
  try {
    await backend.createScanJob()
    ElMessage.success('已创建轻量扫描任务')
    await load()
  } catch (exc) {
    const err = exc as ApiClientError
    ElMessage.error(`${err.message}${err.requestId ? ` (${err.requestId})` : ''}`)
  }
}

onMounted(load)
</script>

<style scoped>
.action-row {
  margin-top: 14px;
}
</style>
