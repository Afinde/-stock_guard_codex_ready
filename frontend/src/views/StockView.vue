<template>
  <div class="page" v-loading="loading">
    <ErrorState :message="error" :request-id="requestId" />
    <section class="panel">
      <div class="toolbar">
        <el-input v-model="symbol" placeholder="600519.SH" style="width: 180px" />
        <el-button type="primary" @click="load">查看</el-button>
      </div>
    </section>
    <section class="panel">
      <h2 class="panel-title">K线</h2>
      <el-empty v-if="!bars.length" description="暂无已持久化行情" />
      <ChartBox v-else :option="klineOption" />
    </section>
    <section class="panel">
      <h2 class="panel-title">最新信号</h2>
      <el-empty v-if="!overview?.latest_signal" description="暂无信号" />
      <el-descriptions v-else :column="3" border>
        <el-descriptions-item label="信号">{{ overview.latest_signal.signal_type }}</el-descriptions-item>
        <el-descriptions-item label="评分">{{ overview.latest_signal.total_score }}</el-descriptions-item>
        <el-descriptions-item label="策略">{{ overview.latest_signal.strategy_version }}</el-descriptions-item>
      </el-descriptions>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import ChartBox from '@/components/ChartBox.vue'
import ErrorState from '@/components/ErrorState.vue'
import { ApiClientError, backend } from '@/api/client'
import type { Bar, Signal } from '@/api/types'

const route = useRoute()
const symbol = ref(String(route.params.symbol || '600519.SH'))
const bars = ref<Bar[]>([])
const overview = ref<{ latest_signal: Signal | null } | null>(null)
const loading = ref(false)
const error = ref('')
const requestId = ref('')

const klineOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  grid: [{ left: 50, right: 20, top: 20, height: 170 }, { left: 50, right: 20, top: 220, height: 60 }],
  xAxis: [{ type: 'category', data: bars.value.map((b) => b.trading_date) }, { type: 'category', gridIndex: 1, data: bars.value.map((b) => b.trading_date) }],
  yAxis: [{ scale: true }, { gridIndex: 1 }],
  series: [
    { type: 'candlestick', data: bars.value.map((b) => [b.open, b.close, b.low, b.high]) },
    { type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: bars.value.map((b) => b.volume) }
  ]
}))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [barResult, overviewResult] = await Promise.all([backend.bars(symbol.value, { limit: 250 }), backend.stockOverview(symbol.value)])
    bars.value = barResult.items
    overview.value = overviewResult
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
