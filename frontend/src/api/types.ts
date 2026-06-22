export interface ApiEnvelope<T> {
  success: boolean
  data: T
  request_id: string
  environment: string
}

export interface Capabilities {
  server_backtest: boolean
  light_scan: boolean
  paper_order_write: boolean
  live_provider: boolean
  live_order: boolean
  websocket: boolean
}

export interface Page<T> {
  items: T[]
  page: number
  page_size: number
  total: number
}

export interface Signal {
  signal_id: number
  symbol: string
  name: string
  signal_type: string
  total_score: number
  score_breakdown: Record<string, unknown>
  recommended_position: number
  stop_loss_reference: string | number | null
  take_profit_reference: Record<string, string | number | null>
  reasons: string[]
  invalidation_conditions: string[]
  strategy_version: string | null
  parameter_digest: string | null
  provider: string | null
  data_checksum: string | null
  generated_at: string
  research_only: boolean
}

export interface DashboardSummary {
  environment: string
  deployment_profile: string
  app_version: string
  data_mode: string
  provider_status: string
  latest_scan_at: string
  latest_trading_date: string
  total_signals: number
  buy_watch_count: number
  hold_count: number
  sell_watch_count: number
  paper_equity: string | null
  paper_cash: string | null
  paper_market_value: string | null
  paper_exposure: string | null
  paper_total_return: string | null
  paper_max_drawdown: string | null
  open_order_count: number
  position_count: number
  latest_job: Job | null
  migration_status: { current_revision: string | null; head_revision: string; migration_required: boolean }
  capabilities: Capabilities
}

export interface Job {
  job_id: string
  task_type: string
  status: string
  attempt: number
  started_at: string
  completed_at: string
}

export interface Bar {
  trading_date: string
  open: string
  high: string
  low: string
  close: string
  volume: number
  amount: string | null
  ma20: number | null
  ma60: number | null
  provider: string
  checksum: string
}
