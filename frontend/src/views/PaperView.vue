<template>
  <div class="page" v-loading="loading">
    <ErrorState :message="error" :request-id="requestId" />
    <el-alert type="warning" :closable="false" title="本页全部为模拟账户数据，不是真实账户资产或真实收益。" />
    <section class="panel">
      <h2 class="panel-title">模拟账户</h2>
      <el-empty v-if="!accounts.length" description="暂无模拟账户" />
      <el-table v-else :data="accounts" @row-click="selectAccount">
        <el-table-column prop="account_id" label="账户" />
        <el-table-column prop="status" label="状态" />
        <el-table-column prop="cash_available" label="模拟现金" />
        <el-table-column prop="market_value" label="模拟市值" />
        <el-table-column prop="total_equity" label="模拟权益" />
      </el-table>
    </section>
    <section class="panel" v-if="accountId">
      <h2 class="panel-title">模拟权益曲线</h2>
      <el-empty v-if="!curve.length" description="暂无权益曲线" />
      <ChartBox v-else :option="curveOption" />
    </section>
    <section class="panel" v-if="accountId">
      <h2 class="panel-title">当前持仓</h2>
      <el-table :data="positions" height="220">
        <el-table-column prop="symbol" label="股票" />
        <el-table-column prop="total_quantity" label="总数量" />
        <el-table-column prop="available_quantity" label="可用数量" />
        <el-table-column prop="average_cost" label="模拟成本" />
        <el-table-column prop="market_value" label="模拟市值" />
      </el-table>
    </section>
    <section class="panel" v-if="accountId">
      <h2 class="panel-title">模拟订单与成交</h2>
      <el-table :data="orders" height="180">
        <el-table-column prop="paper_order_id" label="订单" />
        <el-table-column prop="symbol" label="股票" />
        <el-table-column prop="side" label="方向" />
        <el-table-column prop="status" label="状态" />
      </el-table>
      <el-table :data="fills" height="180">
        <el-table-column prop="fill_id" label="成交" />
        <el-table-column prop="symbol" label="股票" />
        <el-table-column prop="quantity" label="数量" />
        <el-table-column prop="execution_price" label="模拟成交价" />
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
const accounts = ref<Array<Record<string, unknown>>>([])
const accountId = ref('')
const positions = ref<Array<Record<string, unknown>>>([])
const orders = ref<Array<Record<string, unknown>>>([])
const fills = ref<Array<Record<string, unknown>>>([])
const curve = ref<Array<Record<string, unknown>>>([])

const curveOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  xAxis: { type: 'category', data: curve.value.map((row) => row.date) },
  yAxis: { type: 'value', scale: true },
  series: [{ name: '模拟权益', type: 'line', smooth: true, data: curve.value.map((row) => row.total_equity) }]
}))

async function load() {
  loading.value = true
  error.value = ''
  try {
    accounts.value = (await backend.accounts()).items
    if (accounts.value[0]) await selectAccount(accounts.value[0])
  } catch (exc) {
    const err = exc as ApiClientError
    error.value = err.message
    requestId.value = err.requestId
  } finally {
    loading.value = false
  }
}

async function selectAccount(row: Record<string, unknown>) {
  accountId.value = String(row.account_id)
  const [pos, ord, fill, equity] = await Promise.all([
    backend.positions(accountId.value),
    backend.orders(accountId.value),
    backend.fills(accountId.value),
    backend.equityCurve(accountId.value)
  ])
  positions.value = pos.items
  orders.value = ord.items
  fills.value = fill.items
  curve.value = equity.items
}

onMounted(load)
</script>
