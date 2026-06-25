<template>
  <div class="page" v-loading="loading">
    <ErrorState :message="error" />
    <section class="metrics">
      <div class="metric">
        <div class="metric-label">Provider状态</div>
        <div class="metric-value">{{ providerStatus }}</div>
      </div>
      <div class="metric">
        <div class="metric-label">最新数据时间</div>
        <div class="metric-value">{{ latestMarketTime || 'EMPTY' }}</div>
      </div>
      <div class="metric">
        <div class="metric-label">上涨/下跌</div>
        <div class="metric-value">{{ upCount }} / {{ downCount }}</div>
      </div>
      <div class="metric">
        <div class="metric-label">成交额</div>
        <div class="metric-value">{{ amountTotal }}</div>
      </div>
    </section>
    <section class="panel">
      <div class="toolbar">
        <el-button @click="load">刷新</el-button>
        <el-button v-if="auth.isAdmin" type="primary" @click="runJob('market-spot-sync')">触发行情采集</el-button>
        <el-button v-if="auth.isAdmin" @click="runJob('industry-sync')">触发行业采集</el-button>
      </div>
    </section>
    <section class="panel">
      <h2 class="panel-title">当日行情</h2>
      <el-table :data="quotes" height="320">
        <el-table-column prop="symbol" label="股票" />
        <el-table-column prop="close" label="最新价" />
        <el-table-column prop="volume" label="成交量" />
        <el-table-column prop="amount" label="成交额" />
        <el-table-column prop="market_time" label="行情时间" />
        <el-table-column prop="quality_status" label="质量" />
      </el-table>
    </section>
    <section class="panel">
      <h2 class="panel-title">采集任务</h2>
      <el-alert
        v-if="hasPendingJobs"
        title="采集任务已进入队列，需 job-runner 或手动执行本地任务消费后才会开始写入采集运行记录。"
        type="warning"
        :closable="false"
        show-icon
        class="job-alert"
      />
      <el-table :data="jobs" height="220">
        <el-table-column prop="job_id" label="任务" min-width="180" />
        <el-table-column prop="task_type" label="类型" />
        <el-table-column prop="status" label="队列状态" />
        <el-table-column prop="started_at" label="创建/开始" />
        <el-table-column prop="completed_at" label="完成" />
      </el-table>
    </section>
    <section class="panel">
      <h2 class="panel-title">采集运行记录</h2>
      <el-table :data="runs" height="260">
        <el-table-column prop="job_type" label="类型" />
        <el-table-column prop="provider" label="Provider" />
        <el-table-column prop="status" label="状态" />
        <el-table-column prop="success_count" label="成功" />
        <el-table-column prop="duplicate_count" label="重复" />
        <el-table-column prop="error_count" label="错误" />
        <el-table-column prop="started_at" label="开始" />
      </el-table>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import ErrorState from '@/components/ErrorState.vue'
import { ApiClientError, backend } from '@/api/client'
import type { Job, MarketQuote } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const loading = ref(false)
const error = ref('')
const quotes = ref<MarketQuote[]>([])
const runs = ref<Array<Record<string, unknown>>>([])
const jobs = ref<Job[]>([])
const providerStatus = ref('NOT_CONFIGURED')
let pollTimer: number | undefined
let pollDeadline = 0

const latestMarketTime = computed(() => quotes.value[0]?.market_time || '')
const upCount = computed(() => quotes.value.filter((row) => Number(row.close) >= Number(row.open)).length)
const downCount = computed(() => quotes.value.filter((row) => Number(row.close) < Number(row.open)).length)
const amountTotal = computed(() => quotes.value.reduce((sum, row) => sum + Number(row.amount || 0), 0).toFixed(2))
const hasPendingJobs = computed(() => jobs.value.some((job) => ['QUEUED', 'RUNNING'].includes(job.status)))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [quotePage, status, runPage, jobPage] = await Promise.all([backend.marketQuotes({ page: 1, page_size: 50 }), backend.providerStatus(), backend.ingestionRuns(), backend.jobs()])
    quotes.value = quotePage.items
    providerStatus.value = status.status
    runs.value = runPage.items
    jobs.value = jobPage.items.filter((job) => ['MARKET_SPOT_SYNC', 'INDUSTRY_SYNC', 'STOCK_NEWS_SYNC', 'DAILY_BAR_SYNC', 'FINANCIAL_SYNC', 'INSTRUMENT_SYNC'].includes(job.task_type))
  } catch (exc) {
    error.value = (exc as ApiClientError).message
  } finally {
    loading.value = false
  }
}

async function runJob(jobType: string) {
  try {
    const job = await backend.runDataJob(jobType)
    ElMessage.success(`已创建采集任务：${job.status}`)
    await load()
    startPolling()
  } catch (exc) {
    ElMessage.error((exc as ApiClientError).message)
  }
}

function startPolling() {
  pollDeadline = Date.now() + 20_000
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = window.setInterval(async () => {
    await load()
    if (!hasPendingJobs.value || Date.now() > pollDeadline) stopPolling()
  }, 2500)
}

function stopPolling() {
  if (pollTimer) window.clearInterval(pollTimer)
  pollTimer = undefined
}

onMounted(load)
onBeforeUnmount(stopPolling)
</script>

<style scoped>
.job-alert {
  margin-bottom: 12px;
}
</style>
