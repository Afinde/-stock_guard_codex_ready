# Market Data Admission Review

Generate the review package after at least 10 complete Shadow trading days:

```bash
python -m app.market_data_runtime --generate-admission-review
```

The command writes JSON and Markdown files under `data/admission_reviews/` by
default. The package includes:

- provider identity and version;
- usage authorization placeholder;
- field contract version;
- connectivity and authentication results;
- complete trading-day count;
- per-day quality metrics;
- aggregate quality metrics;
- threshold checks;
- degradation, outage, and schema-change events;
- quote recording and replay consistency references;
- ShadowDecision statistics;
- account, fill, order, position, and ledger immutability evidence;
- current admission status;
- unresolved issues.

The system never writes an automatic approval conclusion. A human reviewer must
choose one of:

- `REJECTED`;
- `CONTINUE_OBSERVING`;
- `APPROVED_FOR_LIMITED_PAPER_REVIEW`.

Even after `ELIGIBLE_FOR_REVIEW`, `MARKET_LIVE_SHADOW_MODE` remains true.
Admission review does not enable real orders, simulated fills, order status
flow, real-account access, broker interfaces, or account-return reporting.

Current scope explicitly excludes capacity, stress, high availability, and
disaster recovery validation, so approval for limited paper review must not be
presented as production readiness.
