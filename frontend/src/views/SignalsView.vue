<template>
  <div class="page" v-loading="loading">
    <ErrorState :message="error" :request-id="requestId" />
    <section class="panel">
      <div class="toolbar">
        <el-input v-model="filters.symbol" placeholder="股票代码" style="width: 150px" clearable />
        <el-select v-model="filters.signal_type" placeholder="信号类型" style="width: 170px" clearable>
          <el-option v-for="item in signalTypes" :key="item" :label="item" :value="item" />
        </el-select>
        <el-input-number v-model="filters.minimum_score" :min="0" :max="100" placeholder="最低评分" />
        <el-button type="primary" @click="load">查询</el-button>
      </div>
    </section>
    <section class="panel">
      <el-table :data="rows" @row-click="openDetail">
        <el-table-column prop="symbol" label="股票" width="110" />
        <el-table-column prop="signal_type" label="信号" width="130" />
        <el-table-column label="评分" width="180">
          <template #default="{ row }">
            <el-progress :percentage="Number(row.total_score || 0)" />
          </template>
        </el-table-column>
        <el-table-column prop="strategy_version" label="策略版本" width="120" />
        <el-table-column prop="provider" label="Provider" width="150" />
        <el-table-column prop="generated_at" label="生成时间" />
      </el-table>
      <el-pagination v-model:current-page="pageNo" v-model:page-size="pageSize" :total="total" layout="prev, pager, next, sizes, total" @change="load" />
    </section>
    <el-drawer v-model="drawer" title="信号详情" size="520px">
      <el-descriptions v-if="selected" :column="1" border>
        <el-descriptions-item label="股票">{{ selected.symbol }}</el-descriptions-item>
        <el-descriptions-item label="信号">{{ selected.signal_type }}</el-descriptions-item>
        <el-descriptions-item label="评分">{{ selected.total_score }}</el-descriptions-item>
        <el-descriptions-item label="策略">{{ selected.strategy_version }}</el-descriptions-item>
        <el-descriptions-item label="参数摘要">{{ selected.parameter_digest }}</el-descriptions-item>
        <el-descriptions-item label="数据校验">{{ selected.data_checksum }}</el-descriptions-item>
      </el-descriptions>
      <h3>理由</h3>
      <el-tag v-for="reason in selected?.reasons || []" :key="reason" class="tag-line">{{ reason }}</el-tag>
      <h3>失效条件</h3>
      <el-tag v-for="item in selected?.invalidation_conditions || []" :key="item" type="warning" class="tag-line">{{ item }}</el-tag>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import ErrorState from '@/components/ErrorState.vue'
import { ApiClientError, backend } from '@/api/client'
import type { Signal } from '@/api/types'

const signalTypes = ['BUY_WATCH', 'HOLD', 'REDUCE', 'SELL', 'RISK_OFF', 'DATA_ERROR']
const loading = ref(false)
const error = ref('')
const requestId = ref('')
const rows = ref<Signal[]>([])
const pageNo = ref(1)
const pageSize = ref(20)
const total = ref(0)
const drawer = ref(false)
const selected = ref<Signal | null>(null)
const filters = reactive<{ symbol: string; signal_type: string; minimum_score: number | undefined }>({ symbol: '', signal_type: '', minimum_score: undefined })

async function load() {
  loading.value = true
  error.value = ''
  try {
    const page = await backend.signals({ page: pageNo.value, page_size: pageSize.value, ...filters })
    rows.value = page.items
    total.value = page.total
  } catch (exc) {
    const err = exc as ApiClientError
    error.value = err.message
    requestId.value = err.requestId
  } finally {
    loading.value = false
  }
}

function openDetail(row: Signal) {
  selected.value = row
  drawer.value = true
}

onMounted(load)
</script>
