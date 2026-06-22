# LIVE_PROVIDER Setup

This project only supports real-time market data in PAPER_TRADING Shadow mode.
It must not create fills, modify paper orders, mutate accounts or positions, or
write trading ledger entries.

## Required Environment

```bash
MARKET_DATA_MODE=LIVE_PAPER
MARKET_LIVE_PROVIDER=live_paper
MARKET_LIVE_ENABLED=true
MARKET_LIVE_API_BASE_URL=https://provider.example
MARKET_LIVE_API_KEY=...
MARKET_LIVE_API_SECRET=...
MARKET_LIVE_ACCOUNT_ID=...
MARKET_LIVE_SHADOW_MODE=true
MARKET_LIVE_FAIL_CLOSED=true
```

Secrets must stay in environment variables. They are not written to logs, API
responses, daily reports, or admission review packages.

## Connectivity Test

```bash
bash scripts/test_live_provider.sh
```

If the provider is not configured, the script returns non-zero and prints
`PROVIDER_NOT_CONFIGURED`. A configured run prints a redacted quote-only summary:

```text
environment=PAPER_TRADING
mode=SHADOW
provider_configured=true
network_connectivity=PASSED
authentication=PASSED
quotes_received=2
valid_quotes=2
fills_created=0
orders_modified=0
accounts_modified=0
positions_modified=0
ledger_entries_created=0
```

A successful connectivity test is not production admission. It is only the
first gate before the 10 complete Shadow trading-day observation period.
