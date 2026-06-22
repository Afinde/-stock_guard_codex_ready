import axios, { AxiosError } from 'axios'
import type { ApiEnvelope, Bar, DashboardSummary, Job, Page, Signal } from './types'

export const api = axios.create({
  baseURL: '/api/v1',
  timeout: 15000
})

export class ApiClientError extends Error {
  requestId = ''
  code = 'NETWORK_ERROR'
  status = 0
}

api.interceptors.response.use(
  (response) => response,
  (error: AxiosError<{ error?: { code: string; message: string }; request_id?: string }>) => {
    const wrapped = new ApiClientError(error.response?.data?.error?.message || error.message)
    wrapped.status = error.response?.status || 0
    wrapped.code = error.response?.data?.error?.code || wrapped.code
    wrapped.requestId = error.response?.data?.request_id || ''
    return Promise.reject(wrapped)
  }
)

async function unwrap<T>(promise: Promise<{ data: ApiEnvelope<T> }>): Promise<T> {
  const response = await promise
  return response.data.data
}

export const backend = {
  capabilities: () => unwrap<Record<string, boolean>>(api.get('/capabilities')),
  dashboard: () => unwrap<DashboardSummary>(api.get('/dashboard/summary')),
  signals: (params: Record<string, unknown>) => unwrap<Page<Signal>>(api.get('/signals', { params })),
  signal: (id: number) => unwrap<Signal>(api.get(`/signals/${id}`)),
  bars: (symbol: string, params: Record<string, unknown>) => unwrap<{ symbol: string; status: string; items: Bar[] }>(api.get(`/stocks/${symbol}/bars`, { params })),
  stockOverview: (symbol: string) => unwrap<{ symbol: string; status: string; latest_signal: Signal | null }>(api.get(`/stocks/${symbol}/overview`)),
  stockSignals: (symbol: string) => unwrap<Page<Signal>>(api.get(`/stocks/${symbol}/signals`)),
  backtests: (params: Record<string, unknown>) => unwrap<Page<Record<string, unknown>>>(api.get('/backtests', { params })),
  backtest: (id: string) => unwrap<Record<string, unknown>>(api.get(`/backtests/${id}`)),
  backtestCurve: (id: string) => unwrap<{ items: Array<Record<string, unknown>> }>(api.get(`/backtests/${id}/equity-curve`)),
  backtestTrades: (id: string) => unwrap<Page<Record<string, unknown>>>(api.get(`/backtests/${id}/trades`)),
  accounts: () => unwrap<{ items: Array<Record<string, unknown>> }>(api.get('/paper/accounts')),
  positions: (accountId: string) => unwrap<{ items: Array<Record<string, unknown>> }>(api.get(`/paper/accounts/${accountId}/positions`)),
  orders: (accountId: string) => unwrap<{ items: Array<Record<string, unknown>> }>(api.get(`/paper/accounts/${accountId}/orders`)),
  fills: (accountId: string) => unwrap<{ items: Array<Record<string, unknown>> }>(api.get(`/paper/accounts/${accountId}/fills`)),
  equityCurve: (accountId: string) => unwrap<{ items: Array<Record<string, unknown>> }>(api.get(`/paper/accounts/${accountId}/equity-curve`)),
  ledgerSummary: (accountId: string) => unwrap<Record<string, unknown>>(api.get(`/paper/accounts/${accountId}/ledger-summary`)),
  system: () => unwrap<Record<string, unknown>>(api.get('/system/status')),
  jobs: () => unwrap<Page<Job>>(api.get('/system/jobs')),
  createScanJob: () => unwrap<Job>(api.post('/system/jobs/scan'))
}
