# Shadow Soak Runbook

LIVE_PAPER Shadow is a quote-quality and decision-observation mode. It never
executes simulated fills and never changes paper account state.

## Start Shadow Observation

One-shot run:

```bash
python -m app.market_data_runtime --provider live-paper --shadow --once
```

Continuous run:

```bash
python -m app.market_data_runtime --provider live-paper --shadow
```

Generate a daily report without fetching new quotes:

```bash
python -m app.market_data_runtime --provider live-paper --shadow --report-only
```

Check admission progress:

```bash
python -m app.market_data_runtime --admission-status
```

## Complete Shadow Trading Day

A day counts toward the 10-day admission rule only when all critical gates pass:

- the date is a trading day in `TradingCalendar`;
- provider health check and quote ingestion ran in Shadow mode;
- morning and afternoon sessions both have valid Shadow coverage;
- lunch break is not treated as a missing-data failure;
- closing statistics and daily report are generated;
- normalized quote recording exists and Recorded replay is consistent;
- paper account, order, position, fill, and ledger checksums remain unchanged;
- no PaperFill is created;
- no unhandled P0 provider or schema issue remains.

Any failed gate marks the day `INCOMPLETE` and it does not count toward the 10
complete-day requirement.

## Failure Handling

Provider authentication failures, schema changes, quota errors, maintenance, or
network outages fail closed. Record the degradation event, keep Shadow enabled,
and continue observation only after the provider is healthy again. Do not delete
failed days; they are part of the audit trail.

This runbook does not cover capacity, stress, high availability, or disaster
recovery validation.
