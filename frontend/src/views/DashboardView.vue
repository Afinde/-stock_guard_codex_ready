<template>
  <div class="page" v-loading="loading">
    <ErrorState :message="error" :request-id="requestId" />
    <section class="metrics">
      <div class="metric" v-for="item in metrics" :key="item.label">
        <div class="metric-label">{{ item.label }}</div>
        <div class="metric-value">{{ item.value }}</div>
      </div>
    </section>
    <section class="panel">
      <h2 class="panel-title">信号分布</h2>
      <ChartBox :option="signalOption" />
    </section>
    <section class="panel">
      <h2 class="panel-title">最近信号</h2>
      <el-table :data="signals" height="260">
        <el-table-column prop="symbol" label="股票" width="110" />
        <el-table-column prop="signal_type" label="信号" width="130" />
        <el-table-column prop="total_score" label="评分" width="100" />
        <el-table-column prop="strategy_version" label="策略版本" width="120" />
        <el-table-column prop="generated_at" label="生成时间" />
      </el-table>
    </section>
    <section class="panel">
      <h2 class="panel-title">最近任务</h2>
      <el-empty v-if="!summary?.latest_job" description="暂无任务" />
      <el-descriptions v-else :column="3" border>
        <el-descriptions-item label="任务">{{ summary.latest_job.task_type }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ summary.latest_job.status }}</el-descriptions-item>
        <el-descriptions-item label="开始时间">{{ summary.latest_job.started_at }}</el-descriptions-item>
      </el-descriptions>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import ChartBox from '@/components/ChartBox.vue'
import ErrorState from '@/components/ErrorState.vue'
import { ApiClientError, backend } from '@/api/client'
import type { DashboardSummary, Signal } from '@/api/types'

const loading = ref(false)
const error = ref('')
const requestId = ref('')
const summary = ref<DashboardSummary | null>(null)
const signals = ref<Signal[]>([])

const metrics = computed(() => [
  { label: '信号总数', value: summary.value?.total_signals ?? 0 },
  { label: '观察买入', value: summary.value?.buy_watch_count ?? 0 },
  { label: '模拟权益', value: summary.value?.paper_equity ?? 'EMPTY' },
  { label: '模拟现金', value: summary.value?.paper_cash ?? 'EMPTY' },
  { label: '持仓数量', value: summary.value?.position_count ?? 0 },
  { label: '开放订单', value: summary.value?.open_order_count ?? 0 },
  { label: '迁移状态', value: summary.value?.migration_status.migration_required ? 'MIGRATION_REQUIRED' : 'OK' },
  { label: 'Provider', value: summary.value?.provider_status ?? 'NOT_CONFIGURED' }
])

const signalOption = computed(() => ({
  tooltip: {},
  legend: { bottom: 0 },
  series: [
    {
      type: 'pie',
      radius: ['45%', '70%'],
      data: [
        { name: 'BUY_WATCH', value: summary.value?.buy_watch_count ?? 0 },
        { name: 'HOLD', value: summary.value?.hold_count ?? 0 },
        { name: 'SELL/REDUCE', value: summary.value?.sell_watch_count ?? 0 }
      ]
    }
  ]
}))

async function load() {
  loading.value = true
  error.value = ''
  try {
    summary.value = await backend.dashboard()
    signals.value = (await backend.signals({ page: 1, page_size: 8 })).items
  } catch (exc) {
    const err = exc as ApiClientError
    error.value = err.message
    requestId.value = err.requestId
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
