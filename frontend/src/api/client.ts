import axios, { AxiosError } from 'axios'
import type { ApiEnvelope, Bar, CurrentUser, DashboardSummary, Job, MarketQuote, Page, SectorRecommendation, Signal, StockRecommendation } from './types'

export const api = axios.create({
  baseURL: '/api/v1',
  timeout: 15000,
  withCredentials: true
})

export class ApiClientError extends Error {
  requestId = ''
  code = 'NETWORK_ERROR'
  status = 0
}

let refreshPromise: Promise<CurrentUser> | null = null

api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError<{ error?: { code: string; message: string }; request_id?: string }>) => {
    const original = error.config as (typeof error.config & { _retried?: boolean }) | undefined
    if (error.response?.status === 401 && original && !original._retried && !String(original.url || '').includes('/auth/')) {
      original._retried = true
      refreshPromise ||= backend.refresh().finally(() => {
        refreshPromise = null
      })
      await refreshPromise
      return api(original)
    }
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
  login: (username: string, password: string) => unwrap<CurrentUser>(api.post('/auth/login', { username, password })),
  refresh: () => unwrap<CurrentUser>(api.post('/auth/refresh')),
  logout: () => unwrap<{ status: string }>(api.post('/auth/logout')),
  me: () => unwrap<CurrentUser>(api.get('/auth/me')),
  changePassword: (old_password: string, new_password: string) => unwrap<Record<string, unknown>>(api.post('/auth/change-password', { old_password, new_password })),
  users: () => unwrap<Page<Record<string, unknown>>>(api.get('/admin/users')),
  createUser: (payload: Record<string, unknown>) => unwrap<Record<string, unknown>>(api.post('/admin/users', payload)),
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
  createScanJob: () => unwrap<Job>(api.post('/system/jobs/scan')),
  marketQuotes: (params: Record<string, unknown>) => unwrap<Page<MarketQuote>>(api.get('/market/quotes', { params })),
  marketNews: (params: Record<string, unknown>) => unwrap<Page<Record<string, unknown>>>(api.get('/market/news', { params })),
  industries: () => unwrap<Page<Record<string, unknown>>>(api.get('/market/industries')),
  stockRecommendations: (params: Record<string, unknown>) => unwrap<{ phase: string; generated_at: string; research_only: boolean; items: StockRecommendation[] }>(api.get('/recommendations/stocks', { params })),
  sectorRecommendations: (params: Record<string, unknown>) => unwrap<{ phase: string; generated_at: string; research_only: boolean; items: SectorRecommendation[] }>(api.get('/recommendations/sectors', { params })),
  providerStatus: () => unwrap<{ status: string; items: Array<Record<string, unknown>> }>(api.get('/market/provider-status')),
  ingestionRuns: () => unwrap<Page<Record<string, unknown>>>(api.get('/system/ingestion-runs')),
  runDataJob: (jobType: string) => unwrap<Job>(api.post(`/admin/data-jobs/${jobType}/run`))
}
