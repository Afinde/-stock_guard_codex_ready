<template>
  <div class="page" v-loading="loading">
    <ErrorState :message="error" />
    <section class="panel">
      <div class="toolbar">
        <el-segmented v-model="phase" :options="phaseOptions" @change="load" />
        <el-button @click="load">刷新</el-button>
      </div>
      <el-alert title="以下内容仅供人工复核，不构成收益承诺或自动交易指令。" type="warning" :closable="false" show-icon />
    </section>
    <section class="panel">
      <h2 class="panel-title">推荐股票</h2>
      <el-table :data="stocks" height="360">
        <el-table-column prop="symbol" label="代码" width="120" />
        <el-table-column prop="name" label="名称" width="140" />
        <el-table-column prop="sector" label="板块" width="140" />
        <el-table-column prop="signal_type" label="信号" width="120" />
        <el-table-column prop="score" label="分数" width="90" />
        <el-table-column prop="latest_price" label="最新价" width="100" />
        <el-table-column prop="reference_price" label="参考价" width="100" />
        <el-table-column prop="stop_loss_price" label="止损" width="100" />
        <el-table-column prop="suggested_shares" label="建议股数" width="100" />
        <el-table-column label="原因" min-width="260">
          <template #default="{ row }">{{ row.reasons?.join('；') }}</template>
        </el-table-column>
      </el-table>
    </section>
    <section class="panel">
      <h2 class="panel-title">推荐板块</h2>
      <el-table :data="sectors" height="320">
        <el-table-column prop="sector" label="板块" width="160" />
        <el-table-column prop="rank_score" label="排序分" width="100" />
        <el-table-column prop="change_pct" label="涨跌幅" width="110" />
        <el-table-column prop="turnover" label="成交额" width="160" />
        <el-table-column prop="leading_stock" label="领涨股" width="130" />
        <el-table-column prop="market_time" label="行情时间" width="210" />
        <el-table-column prop="reason" label="推荐依据" min-width="260" />
      </el-table>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import ErrorState from '@/components/ErrorState.vue'
import { ApiClientError, backend } from '@/api/client'
import type { SectorRecommendation, StockRecommendation } from '@/api/types'

const phase = ref('pre_market')
const phaseOptions = [
  { label: '开盘前', value: 'pre_market' },
  { label: '闭市后', value: 'post_market' }
]
const loading = ref(false)
const error = ref('')
const stocks = ref<StockRecommendation[]>([])
const sectors = ref<SectorRecommendation[]>([])

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [stockData, sectorData] = await Promise.all([
      backend.stockRecommendations({ phase: phase.value, limit: 30 }),
      backend.sectorRecommendations({ phase: phase.value, limit: 30 })
    ])
    stocks.value = stockData.items
    sectors.value = sectorData.items
  } catch (exc) {
    error.value = (exc as ApiClientError).message
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>
