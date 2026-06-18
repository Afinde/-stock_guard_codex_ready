# AGENTS.md — Stock Guard Codex Development Rules

## 1. Mission

This repository is an A-share research, screening, alerting, risk-control, backtesting, and paper-trading project.
It is not a guaranteed-profit system and must never claim or imply guaranteed returns.

The default product boundary is:

- market-data ingestion and validation;
- explainable stock screening and signal generation;
- entry, exit, stop-loss, take-profit, and position-size suggestions;
- alerts, audit logs, backtests, and paper trading;
- manual confirmation before any real-world order.

## 2. Non-negotiable safety constraints

1. Never promise daily, monthly, or annual returns.
2. Never optimize for “5%-8% daily guaranteed profit.” Treat 5% and 8% only as configurable take-profit observation levels.
3. Keep `ENABLE_LIVE_ORDER=false` by default.
4. Keep `MANUAL_CONFIRM_REQUIRED=true` by default.
5. Do not implement broker login automation, CAPTCHA bypass, password capture, simulated mouse clicking, or undocumented broker APIs.
6. A live broker adapter may only be added behind an explicit interface, feature flag, audit log, risk checks, and human approval.
7. Data errors, missing bars, stale timestamps, schema drift, or provider failures must fail closed and produce `DATA_ERROR` or `RISK_OFF`; they must never silently become buy signals.
8. Never use future data in a signal or backtest. Prevent look-ahead bias, survivorship bias, and constituent leakage.
9. Do not weaken risk limits merely to improve backtest results.
10. Secrets must come from environment variables and must never be printed to logs, committed, or included in test fixtures.

## 3. Repository map

- `app/config.py`: application and risk settings.
- `app/data_provider.py`: market-data provider and normalization.
- `app/strategy.py`: indicators, scoring, position sizing, and exits.
- `app/service.py`: orchestration and persistence.
- `app/db.py`: SQLAlchemy models and database setup.
- `app/main.py`: FastAPI entry point.
- `app/scheduler.py`: scheduled scans and alerts.
- `app/notifier.py`: notification adapter.
- `tests/`: automated tests.
- `docs/ROADMAP.md`: milestone sequence and scope.
- `docs/CODEX_PROMPTS.md`: reusable task prompts.
- `docs/DEFINITION_OF_DONE.md`: completion criteria.

## 4. Required workflow for every task

Before editing:

1. Read this file, `README.md`, and the relevant source and test files.
2. Restate the task, assumptions, out-of-scope items, and risk impact.
3. Run the current verification command.
4. Propose a small implementation plan.

While editing:

1. Keep the change focused; do not rewrite unrelated modules.
2. Prefer typed, testable, dependency-injected code.
3. Separate data, strategy, risk, execution, persistence, and presentation concerns.
4. Add or update tests for success, failure, boundary, and regression cases.
5. Preserve backward compatibility unless the task explicitly requires a breaking change.

After editing:

1. Run `bash scripts/verify.sh`.
2. Report changed files and why each changed.
3. Report tests run and results.
4. Report unresolved risks, TODOs, and manual verification steps.
5. Do not claim completion if verification failed.

## 5. Commands

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
bash scripts/verify.sh
uvicorn app.main:app --reload
python -m app.scheduler
```

## 6. Architecture rules

### Market data

- Put provider-specific code behind a provider abstraction.
- Validate required columns, types, symbol, bar order, duplicate timestamps, OHLC relationships, stale data, and minimum history.
- Make adjustment mode explicit and consistent.
- Add retry, timeout, rate-limit, and cache behavior without hiding permanent failures.
- Use Asia/Shanghai for exchange timestamps.

### Strategy

- Signals must be deterministic for identical inputs and configuration.
- Signals must include explanation, trigger conditions, invalidation conditions, reference price, stop price, and strategy version.
- Large-language-model output may summarize information but may not be the sole source of a buy or sell decision.
- Factor weights and thresholds should be configuration, not hidden constants.

### Risk

- Risk checks take precedence over strategy signals.
- Position sizing must enforce account risk, board-lot rules, available cash, single-position cap, total-position cap, industry concentration, daily loss, drawdown, and consecutive-loss limits.
- Include gap-risk warnings; a 5% stop price does not guarantee a 5% realized loss.
- New-entry signals must be blocked when the portfolio is in `RISK_OFF`.

### Backtesting

- Use event time and only information available at that time.
- Model commissions, stamp duty, slippage, T+1, suspension, price limits, partial fills, rejected orders, and liquidity constraints.
- Store data version, universe version, strategy version, parameters, and run ID.
- Report annualized return, maximum drawdown, Sharpe, Calmar, win rate, payoff ratio, turnover, exposure, and consecutive losses.
- Include walk-forward and out-of-sample tests before interpreting results.

### Paper and live execution

- Implement paper execution before any broker adapter.
- Broker integration must use a `BrokerAdapter` interface and remain disabled by default.
- Every order lifecycle event must be auditable and replayable.
- Never place an order from a notification callback.

## 7. Test expectations

At minimum, add tests for:

- invalid and normalized symbols;
- insufficient, stale, duplicated, and malformed market data;
- indicator warm-up and NaN handling;
- score thresholds and deterministic signals;
- A-share board-lot sizing and zero/negative inputs;
- stop-loss, take-profit, gap, and trailing-stop boundaries;
- total exposure and portfolio `RISK_OFF` behavior;
- provider failure and database failure paths;
- scheduler deduplication and non-trading-day behavior;
- backtest anti-look-ahead checks.

Network-dependent tests must be isolated or mocked. The default verification suite must run without internet access.

## 8. Scope control

For large requests, split work into milestones and complete only one milestone per change set. Prefer small reviewable commits such as:

- `feat(data): add validated market-data provider interface`
- `feat(risk): enforce portfolio exposure limits`
- `test(strategy): add score-boundary regression cases`
- `docs(codex): document paper-trading milestone`

Do not combine a provider migration, strategy rewrite, database migration, frontend build, and broker integration in one task.
