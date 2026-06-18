# Reusable Codex Prompts

Use one prompt at a time. Require Codex to read `AGENTS.md` first and keep each change reviewable.

## Prompt 1 — Baseline repository audit

```text
Read AGENTS.md, README.md, all files under app/, and tests/. Do not modify code yet.
Audit the repository for correctness, security, maintainability, financial-risk controls, data-quality risks, future-data leakage, test gaps, and deployment problems.
Rank findings as P0/P1/P2. For each finding, cite the exact file and function, explain impact, and propose the smallest safe fix.
Then propose a milestone plan. Do not implement anything until the audit is complete.
```

## Prompt 2 — First implementation task

```text
Read AGENTS.md and docs/ROADMAP.md. Implement only Milestone 1's provider abstraction and offline data-validation tests.
Do not add a frontend, backtester, paper trader, or broker integration.
Before editing, run the current verification suite and summarize the baseline.
Requirements:
- introduce a typed MarketDataProvider abstraction;
- keep AKShare behind an adapter;
- add validation for required columns, numeric values, timestamp ordering, duplicates, OHLC relationships, stale data, and minimum history;
- make adjustment mode explicit;
- use mocks/fixtures so default tests do not need internet;
- preserve current API behavior unless a breaking change is necessary and documented.
After editing, run bash scripts/verify.sh and report changed files, test results, and remaining risks.
```

## Prompt 3 — Strategy configuration

```text
Read AGENTS.md and implement only the configurable strategy portion of Milestone 2.
Move factor weights and thresholds out of hidden constants into a validated, versioned configuration model.
Keep signal generation deterministic and backward compatible.
Add tests for every scoring boundary, NaN warm-up behavior, insufficient history, and the exact 70-point action threshold.
Do not optimize parameters against historical returns in this task.
Run bash scripts/verify.sh before and after the change.
```

## Prompt 4 — Portfolio risk engine

```text
Read AGENTS.md and docs/ROADMAP.md. Design and implement the smallest portfolio risk engine needed for Milestone 3.
Risk rules must override strategy signals. Enforce available cash, board-lot rounding, per-trade risk, single-position cap, total exposure, daily loss, consecutive losses, and drawdown-based RISK_OFF.
A RISK_OFF state must block new entries but permit exits that reduce risk.
Return structured reason codes and readable explanations.
Add focused unit tests and do not implement a broker adapter.
```

## Prompt 5 — Event-driven backtest design only

```text
Read AGENTS.md and the current code. Do not implement the backtester yet.
Produce a technical design for Milestone 4 covering events, state transitions, time semantics, T+1, price limits, suspension, partial fills, fees, slippage, data/version lineage, reproducibility, and anti-look-ahead tests.
Show proposed modules, interfaces, database entities, and migration sequence.
Identify which assumptions require user confirmation.
```

## Prompt 6 — Pull-request review

```text
Review the current branch against its base branch and AGENTS.md.
Focus on functional bugs, financial-risk bypasses, data leakage, unsafe defaults, missing error handling, nondeterminism, security issues, and inadequate tests.
Do not praise style. List findings in severity order with file and line references, then list test gaps and residual risks.
If there are no findings, say so explicitly and describe what you verified.
```
