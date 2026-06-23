<template>
  <div class="page" v-loading="loading">
    <ErrorState :message="error" :request-id="requestId" />
    <el-alert type="info" :closable="false" title="历史回测仅用于研究复盘，不代表未来表现。" />
    <section class="panel">
      <el-table :data="rows" @row-click="selectRun">
        <el-table-column prop="backtest_id" label="Run ID" min-width="190" />
        <el-table-column prop="strategy_version" label="策略版本" width="120" />
        <el-table-column prop="status" label="状态" width="120" />
        <el-table-column prop="total_return" label="历史收益" width="130" />
        <el-table-column prop="maximum_drawdown" label="最大回撤" width="130" />
        <el-table-column prop="created_at" label="创建时间" />
      </el-table>
      <el-pagination v-model:current-page="pageNo" :total="total" layout="prev, pager, next, total" @change="load" />
    </section>
    <section class="panel" v-if="selected">
      <h2 class="panel-title">回测详情</h2>
      <el-descriptions :column="4" border>
        <el-descriptions-item label="初始资金">{{ selected.initial_cash }}</el-descriptions-item>
        <el-descriptions-item label="最终权益">{{ selected.final_equity }}</el-descriptions-item>
        <el-descriptions-item label="胜率">{{ selected.win_rate }}</el-descriptions-item>
        <el-descriptions-item label="交易次数">{{ selected.trade_count }}</el-descriptions-item>
      </el-descriptions>
      <ChartBox :option="curveOption" />
      <el-table :data="trades" height="240">
        <el-table-column prop="session_date" label="日期" />
        <el-table-column prop="symbol" label="股票" />
        <el-table-column prop="side" label="方向" />
        <el-table-column prop="quantity" label="数量" />
        <el-table-column prop="execution_price" label="成交价" />
      </el-table>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import ChartBox from '@/components/ChartBox.vue'
import ErrorState from '@/components/ErrorState.vue'
import { ApiClientError, backend } from '@/api/client'

const loading = ref(false)
const error = ref('')
const requestId = ref('')
const rows = ref<Array<Record<string, unknown>>>([])
const selected = ref<Record<string, unknown> | null>(null)
const curve = ref<Array<Record<string, unknown>>>([])
const trades = ref<Array<Record<string, unknown>>>([])
const pageNo = ref(1)
const total = ref(0)

const curveOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  legend: { bottom: 0 },
  xAxis: { type: 'category', data: curve.value.map((row) => row.date) },
  yAxis: { type: 'value', scale: true },
  series: [
    { name: '历史权益', type: 'line', smooth: true, data: curve.value.map((row) => row.total_equity) },
    { name: '历史回撤', type: 'line', smooth: true, data: curve.value.map((row) => row.drawdown) }
  ]
}))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const page = await backend.backtests({ page: pageNo.value, page_size: 20 })
    rows.value = page.items
    total.value = page.total
    if (!selected.value && rows.value.length) await selectRun(rows.value[0])
  } catch (exc) {
    const err = exc as ApiClientError
    error.value = err.message
    requestId.value = err.requestId
  } finally {
    loading.value = false
  }
}

async function selectRun(row: Record<string, unknown>) {
  selected.value = row
  const id = String(row.backtest_id)
  const [curveResult, tradesResult] = await Promise.all([backend.backtestCurve(id), backend.backtestTrades(id)])
  curve.value = curveResult.items
  trades.value = tradesResult.items
}

onMounted(load)
</script>
