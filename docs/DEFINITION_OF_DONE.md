# Definition of Done

A task is complete only when all applicable items are satisfied.

## Scope and design

- The implementation matches one clearly stated task or milestone.
- Out-of-scope items are not silently added.
- Assumptions and breaking changes are documented.
- Risk behavior is explicit and fails closed.

## Code

- Code is typed where practical and follows existing module boundaries.
- Provider, strategy, risk, execution, persistence, and API responsibilities remain separated.
- Constants that affect strategy or risk are validated configuration.
- No secret, credential, token, local database, or generated cache is committed.
- Live ordering remains disabled unless the task is a separately approved broker milestone.

## Tests

- Success, boundary, failure, and regression cases are covered.
- Tests are deterministic and run without internet by default.
- Time-dependent logic uses controlled clocks or fixtures.
- No test depends on the current market price or today’s network response.
- `bash scripts/verify.sh` passes.

## Financial correctness

- No claim of guaranteed profit is introduced.
- No future data is used.
- A data error cannot become an actionable signal.
- Risk checks cannot be bypassed by the strategy or notification layer.
- Position sizing respects board lots, cash, account risk, and exposure limits.
- Stop-loss text explains gap and execution risk.

## Operations and audit

- Important events contain timestamps, version IDs, and reason codes.
- Repeated jobs are idempotent where applicable.
- Failures are logged without leaking secrets.
- Manual verification steps and rollback notes are documented.

## Review report

The final Codex response must include:

1. Summary of behavior changed.
2. Files changed.
3. Commands/tests run and results.
4. Known limitations and residual risks.
5. Suggested next task, limited to one milestone-sized change.
