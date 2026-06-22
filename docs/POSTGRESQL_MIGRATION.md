# PostgreSQL Migration Notes

This project currently supports SQLite for local, single-node use and PostgreSQL
for opt-in integration testing of paper-trading concurrency semantics. The
PostgreSQL path now includes Alembic baseline migrations, transaction retry
classification, and paper-fill fault-injection tests. It is not a production
capacity, stress, disaster-recovery, or high-availability validation.

## Current SQLite Design

- Task leases use `task_leases.lease_key` as a unique key.
- Task idempotency uses `scheduled_task_runs.idempotency_key`.
- Order event idempotency uses `paper_order_market_events(paper_order_id, market_event_id)`.
- Fill idempotency uses `paper_fills.fill_idempotency_key`.
- SQLite relies on transactions, uniqueness, and optimistic `version` columns. It does not provide row-level `SKIP LOCKED`.

## PostgreSQL Direction

- Use `READ COMMITTED` for normal task claiming and order batches.
- Use `SELECT ... FOR UPDATE` when a repository needs to claim or heartbeat a row.
- Use `FOR UPDATE SKIP LOCKED` in PostgreSQL task lease and paper-order claim paths so multiple runtime instances can claim independent work without blocking.
- Keep `task_leases.lease_key` unique. A lease takeover should update rows whose `expires_at < now()`.
- Advisory locks may be useful for coarse runtime maintenance tasks, but order processing should prefer row locks plus idempotency constraints.
- Keep optimistic `version` columns on `paper_accounts`, `paper_positions`, and `paper_orders` to detect stale writers.
- Map Decimal string columns to `NUMERIC(24, 8)` only after a tested migration. Until then, string storage remains portable and exact for SQLite.
- Migrate JSON text columns to `JSONB` for queryability: notification payloads, market snapshot payloads, account snapshot positions, and recovery summaries.
- Use `TIMESTAMP WITH TIME ZONE` for all business timestamps.

## Repository Structure

`app/repositories.py` centralizes database-dialect differences:

- `TaskLeaseRepository`: acquire, heartbeat, release, and active-count semantics.
- `ScheduledTaskRepository`: task lookup by idempotency key.
- `PaperOrderRepository`: executable order claiming, including PostgreSQL
  `FOR UPDATE SKIP LOCKED`.
- `MarketQuoteRepository`: latest valid quote reads.
- `SettlementRepository`: day-end snapshot lookup.

Domain services call these repository methods and do not branch on
`postgresql` directly.

SQLite keeps the existing transaction and uniqueness behavior. PostgreSQL uses
SQLAlchemy `with_for_update(skip_locked=True)` in repository methods. Task or
order claiming is a short transaction; order matching and account updates happen
in a separate fill transaction.

## Alembic Migrations

Milestone 5.2A.1 adds Alembic as the schema migration entry point:

```bash
bash scripts/migrate.sh
bash scripts/migration_status.sh
```

Current revisions:

- `20260620_0001`: baseline SQLAlchemy schema.
- `20260622_0002`: LIVE_PAPER Shadow market-data metadata, quote
  comparison, quality metrics, recorded quote index, and expanded shadow
  decision audit fields.

For a fresh database, run `bash scripts/migrate.sh` to apply the baseline. For
an existing database that already matches the baseline schema, first validate the
schema and then stamp the baseline revision:

```bash
python -m alembic stamp 20260620_0001
```

Do not stamp a production database only because tables exist. The required
baseline tables and columns must match the checked-in model metadata first. The
paper-runtime startup path checks non-SQLite databases against the current
Alembic head and fails closed when migration is required.

SQLite remains supported for local development and tests. The legacy `init_db()`
compatibility path is kept for development and old local databases, but
PostgreSQL operators should use Alembic scripts rather than relying on runtime
table creation.

## Locking, Isolation, and Timeouts

- Default PostgreSQL isolation: `READ COMMITTED`.
- Claim transactions: row-level lock with `SKIP LOCKED`, then update owner or
  processing metadata and commit quickly.
- Fill transaction: re-read current order/account/position state, declare
  `paper_order_market_events`, write fill/account/position/ledger/risk/outbox,
  and commit atomically.
- Local and CI integration tests set `PGOPTIONS="-c lock_timeout=2000 -c statement_timeout=15000"`.
- Deadlock (`40P01`) and serialization failure (`40001`) are retried by the
  transaction runner with a fresh database session for each attempt.
- Lock-not-available (`55P03`) is configurable and disabled by default.
- Integrity errors, schema/data errors, configuration errors, and unexpected
  programming exceptions are not retried.

## Transaction Retry And Fault Injection

`app.transactions.TransactionRunner` owns retry behavior at the database
transaction boundary. Every attempt creates a new SQLAlchemy `Session`, rolls
back and closes it on failure, and logs the attempt number, SQLSTATE, exception
type, and retry decision without logging secrets.

`PaperMarketMonitorService` accepts a `PaperFaultInjector` for deterministic
failure tests. Supported injection points include fill insert, account update,
position update, ledger insert, outbox insert, and the pre-commit boundary. The
fault-injection tests verify that a failure leaves no partial fill/account/
position/ledger/outbox state and that a later clean retry can complete exactly
one fill.

## Migration Steps

1. Stop paper runtime workers.
2. Run a schema diff from SQLAlchemy metadata to PostgreSQL DDL in staging.
3. Copy static reference data first, then accounts, positions, orders, fills, ledger, snapshots, task runs, leases, and outbox.
4. Validate row counts and key uniqueness.
5. Recompute account totals from ledger and compare with stored snapshots.
6. Start one runtime instance in recovery-only mode.
7. Enable `MARKET_MONITOR` after recovery reports no blocking issues.

## Dual-Write Or Downtime

- Preferred first migration: downtime migration. The runtime is stopped while data is copied and validated.
- Dual-write is not recommended until order and ledger reconciliation tools are stronger.

## Rollback

- Keep the SQLite database read-only after migration.
- If PostgreSQL validation fails, stop PostgreSQL runtime and restart SQLite runtime from the preserved SQLite database.
- Do not merge divergent paper-trading histories manually.

## Behavior Differences

- SQLite serializes writes broadly; PostgreSQL allows row-level concurrency.
- PostgreSQL can use `SKIP LOCKED`; SQLite cannot.
- PostgreSQL `NUMERIC` and `JSONB` need explicit migrations and validation.
- Timezone behavior must be tested with `TIMESTAMP WITH TIME ZONE`.

## Integration Test Plan

- Two runtime instances claim the same task lease.
- Two runtime instances process the same order batch with `SKIP LOCKED`.
- Duplicate `market_event_id` cannot produce a second fill.
- A transaction failure rolls back order, fill, account, position, ledger, and outbox changes.
- Recovery detects orphan frozen cash, orphan frozen positions, and stuck `RUNNING` tasks.

## Local Test Container

Milestone 5.2 adds an opt-in local PostgreSQL test environment:

```bash
bash scripts/test_postgres.sh
```

The script starts `docker-compose.postgres-test.yml`, uses only the test database
`stock_guard_test`, sets `RUN_POSTGRES_TESTS=1`, runs `tests/integration_postgres`,
and tears the container down with volumes. Ordinary `bash scripts/verify.sh` does
not start Docker and does not require PostgreSQL.

The checked-in credentials are local test-container credentials only. Do not use
them for shared, staging, or production databases.

## Milestone 5.2 / 5.2A.1 Coverage

The current integration tests verify:

- full SQLAlchemy schema creation against PostgreSQL;
- Decimal string round-trip and timezone-aware timestamp round-trip;
- JSON payload storage;
- old `scheduled_task_runs` schema upgrade and repeated `init_db()` migration;
- task lease competition and transaction rollback release;
- PostgreSQL order claim using `FOR UPDATE SKIP LOCKED` with independent
  database sessions;
- concurrent quote snapshot insertion deduped by database uniqueness;
- concurrent paper order processing producing one fill, one position update path,
  and ledger entries.
- fault injection rollback for fill, account, position, ledger, outbox, and
  pre-commit failure points;
- duplicate fill idempotency-key failure rolls back account/position/ledger
  changes;
- real PostgreSQL deadlock behavior and transaction-runner retry classification;
- real PostgreSQL serializable conflict retry with a new session per attempt;
- Alembic upgrade, downgrade, repeat upgrade, and baseline-stamp validation for
  SQLite and PostgreSQL;
- day-end snapshot uniqueness.

Remaining PostgreSQL hardening before production use:

- add migration tests from historical SQLite schemas rather than only fresh schema;
- convert selected money strings to tested `NUMERIC` columns only after an
  explicit migration plan;
- add capacity, long-running soak, failover, backup, and restore validation
  before any production PostgreSQL claim.

## Milestone 5.2B LIVE_PAPER Shadow Boundary

LIVE_PAPER is a quote-only, PAPER_TRADING Shadow mode. It may fetch and record
market quotes, create normalized quote snapshots, update provider health,
compare quote sources, compute data-quality metrics, and write shadow decisions.
It must not create paper fills, mutate paper orders, mutate paper accounts,
mutate paper positions, write trading ledger entries, connect to a broker, or
submit real orders.

The online provider boundary is:

- Provider fetches raw quote payloads only.
- Normalization converts raw payloads to `RealTimeQuoteSnapshot`.
- Only normalized and validated snapshots can be stored.
- Network calls happen outside database transactions.
- `MARKET_MONITOR` reads stored `VALID` quotes and writes ShadowDecision rows
  only when `MARKET_LIVE_SHADOW_MODE=true`.
- `MARKET_DATA_MODE=LIVE_PAPER` with Shadow disabled is rejected at startup.

Default development mode remains:

- `MARKET_DATA_MODE=FIXTURE`
- `MARKET_LIVE_ENABLED=false`
- `MARKET_LIVE_SHADOW_MODE=true`
- `MARKET_LIVE_FAIL_CLOSED=true`

Manual quote-only connectivity can be checked with:

```bash
bash scripts/test_live_provider.sh
```

The script is not part of CI. Without live provider configuration it exits
non-zero and reports that the provider is not configured. A successful run only
proves a small quote request worked once; it is not evidence of long-term
stability, capacity, trading readiness, account connectivity, or execution
quality.

## CI

`.github/workflows/ci.yml` has two jobs:

- `unit-tests`: runs `bash scripts/verify.sh` without Docker or PostgreSQL.
- `postgres-integration-tests`: starts a PostgreSQL 16 service container and
  runs `python -m pytest -q tests/integration_postgres` with
  `RUN_POSTGRES_TESTS=1`.
