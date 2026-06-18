# Development Roadmap

## Product objective

Build an explainable A-share decision-support platform that performs data validation, stock screening, signal monitoring, portfolio risk control, backtesting, paper trading, and alerting. Real orders remain disabled unless a separately reviewed broker-integration phase is completed.

## Milestone 0 — Repository baseline and engineering controls

Priority: P0

Deliverables:

- Initialize Git and protect the main branch.
- Add repeatable verification, environment examples, ignore rules, and developer instructions.
- Document current architecture and known technical debt.
- Establish semantic versioning and changelog discipline.
- Ensure tests are offline and deterministic.

Acceptance:

- A clean checkout can install and run tests using documented commands.
- No secret or local database is tracked.
- Codex can explain the repository and identify module boundaries without modifying code.

## Milestone 1 — Reliable market-data foundation

Priority: P0

Deliverables:

- Introduce a `MarketDataProvider` protocol or abstract base class.
- Move AKShare behind an adapter.
- Add explicit adjustment mode, exchange calendar, timestamps, cache, timeout, retry, and rate limiting.
- Validate schema, ordering, duplicates, OHLC consistency, stale bars, and minimum history.
- Add metadata for provider, fetch time, adjustment mode, and data version.
- Add unit tests using fixtures and mocks.

Acceptance:

- Provider/network failures fail closed.
- Non-trading days do not trigger scans.
- Default tests require no internet.
- The same input bars produce identical normalized output.

## Milestone 2 — Configurable strategy and signal lifecycle

Priority: P0

Deliverables:

- Move factor weights and thresholds into versioned configuration.
- Add `BUY_WATCH`, `BUY_CONFIRM`, `HOLD`, `REDUCE`, `SELL`, `DATA_ERROR`, and `RISK_OFF` states.
- Add trigger, invalidation, risk explanation, input timestamp, and strategy version to each signal.
- Separate entry logic from exit logic.
- Add deduplication and signal expiry.
- Add comprehensive boundary tests.

Acceptance:

- Every signal is reproducible from stored bars and configuration.
- Identical inputs cannot generate different signals.
- A stale or malformed input cannot generate an actionable signal.

## Milestone 3 — Portfolio and risk engine

Priority: P0

Deliverables:

- Model cash, positions, realized/unrealized P&L, exposure, industry exposure, and drawdown.
- Enforce single-trade risk, single-position cap, total exposure, daily loss, consecutive losses, and drawdown rules.
- Add board-lot rounding, available-cash checks, fees, gap-risk messages, and order rejection reasons.
- Make risk checks override strategy recommendations.

Acceptance:

- No proposed order can exceed any configured risk limit.
- `RISK_OFF` blocks new entries but permits risk-reducing exits.
- Risk decisions include machine-readable reason codes and human-readable explanations.

## Milestone 4 — Event-driven backtesting

Priority: P1

Deliverables:

- Create event types for bars, signals, orders, fills, portfolio updates, and risk events.
- Model commission, stamp duty, slippage, suspension, price limits, T+1, partial fills, rejection, and liquidity limits.
- Store run ID, data version, universe version, strategy version, parameters, and results.
- Report annualized return, maximum drawdown, Sharpe, Calmar, win rate, payoff ratio, turnover, exposure, and consecutive losses.
- Add walk-forward, out-of-sample, and market-regime analysis.

Acceptance:

- Anti-look-ahead tests pass.
- Re-running the same versioned inputs produces the same result.
- Results clearly separate in-sample and out-of-sample periods.

## Milestone 5 — Paper trading and monitoring

Priority: P1

Deliverables:

- Add paper account, order, fill, position, cash, fee, and transaction-ledger models.
- Add idempotency keys and replayable event logs.
- Add scheduled pre-market, intraday, and post-market workflows using a real exchange calendar.
- Add alert deduplication, severity, acknowledgement, retry, and delivery status.
- Add operational dashboard/API endpoints.

Acceptance:

- The complete paper account can be reconstructed from the ledger.
- Re-running a scheduler job does not create duplicate orders or alerts.
- Paper trading runs for 8–12 weeks before live integration is considered.

## Milestone 6 — Frontend and reporting

Priority: P2

Deliverables:

- Build a Vue 3 + TypeScript frontend.
- Add overview, candidates, positions, risk center, backtests, audit log, and settings pages.
- Show data timestamp, signal version, risk status, and “research assistance; no guaranteed return” notices.
- Export daily and weekly reports.

Acceptance:

- Users can trace each recommendation to data, rules, and risk checks.
- No page presents a recommendation as guaranteed profit.

## Milestone 7 — Optional broker integration

Priority: P3 and separately approved

Prerequisites:

- Paper trading acceptance completed.
- Broker officially supports the intended API and account permissions.
- Applicable reporting, operational, and security requirements have been confirmed.
- Manual emergency stop and human approval flow are tested.

Deliverables:

- Define a broker-neutral `BrokerAdapter`.
- Implement only an officially documented adapter.
- Add pre-trade and post-trade risk checks, idempotency, reconciliation, audit logs, and kill switch.
- Keep live mode disabled in default configuration and test environments.

Acceptance:

- No credential is stored in source or logs.
- No order can bypass manual confirmation while it is required.
- A failed acknowledgement, stale quote, reconciliation mismatch, or risk failure blocks submission.
