# Backtesting

Milestone 4 implements deterministic historical simulation for research. It does not connect to
real broker accounts and does not create live or paper-trading orders.

## Event Order

Each session executes in a fixed order:

1. `SESSION_START`
2. apply effective corporate actions
3. release T+1 and corporate-action locked shares whose tradable date has arrived
4. receive dividend cash whose payment date has arrived
5. cancel pending orders affected by corporate actions
6. load the current session's validated raw bars
7. `MARKET_OPEN`
8. execute pending orders whose earliest execution session has arrived
9. `BAR`
10. evaluate stop-loss, take-profit, suspension, price-limit and liquidity conditions
11. `SESSION_CLOSE`
12. mark positions to raw close
13. generate signals using data through the current close only
14. convert portfolio to `AccountSnapshot`
15. call `RiskEngine`
16. create orders for the next session only
17. `SETTLEMENT`
18. save daily equity and positions

Signals from session T can execute no earlier than session T+1.

## Price Usage

- Strategy indicators use `signal_price_adjust`.
- Simulated fills, cash P&L, cost basis and valuation use raw unadjusted prices by default.
- If `execution_price_adjust` is not empty, the run is rejected as research-only because corporate
  action adjustment is not fully modeled in this milestone.
- Corporate actions are applied to the raw position ledger rather than by rewriting historical
  execution prices.

## Matching Rules

- `MARKET_ON_NEXT_OPEN` buys use next open plus buy slippage.
- `MARKET_ON_NEXT_OPEN` sells use next open minus sell slippage.
- `LIMIT` buys may fill only when low touches the limit.
- `LIMIT` sells may fill only when high touches the limit.
- Price ticks are rounded conservatively: buys round up, sells round down.
- Quantity is constrained by lot size, cash, available quantity, suspension, price limits and volume participation.
- Same-bar stop-loss and take-profit conflicts default to `WORST_CASE`.

## Fees And Metrics

Fees are configurable and calculated with `Decimal`.

- Buy commission: `max(trade_value * buy_commission_rate, minimum_commission)`.
- Sell commission: `max(trade_value * sell_commission_rate, minimum_commission)`.
- Sell tax: `trade_value * sell_tax_rate`.
- Other fees: side-specific fee rate plus transfer fee.
- Total return: `(final_equity - initial_cash) / initial_cash`.
- Annualized return: compound return over configured annualization days.
- Max drawdown: maximum peak-to-trough daily close equity decline.
- Sharpe: annualized excess mean daily return divided by annualized daily volatility.
- Sortino: annualized excess mean daily return divided by annualized downside volatility.
- Calmar: annualized return divided by max drawdown.
- Turnover: total traded value divided by average daily equity.

NaN and Infinity are converted to `null` rather than being stored or exported.

## Corporate Actions

Offline corporate actions include cash dividends, stock dividends, capitalization, rights issues,
splits, reverse splits, delisting and symbol changes. Strategies can only receive corporate actions
whose `announcement_date` is on or before the current session; future announcements stay hidden.

Cash dividends create an entitlement on `record_date` using the then-held quantity. Cash is received
on `payment_date`, so selling after the record date does not remove the entitlement and buying after
the record date does not create one. Dividend tax is configurable.

Stock dividends and capitalization increase quantity and lower unit cost while preserving total cost,
except for explicit rounding. New shares remain locked until `tradable_date`. Splits and reverse
splits rescale quantity and cost basis with an auditable ledger event. Rights issues default to
`FAIL_CLOSED`; the engine does not assume the investor can or will subscribe.

Pending orders are cancelled on corporate-action ex-dates using `CANCELLED_CORPORATE_ACTION`; order
prices and quantities are not silently adjusted.

`result_quality` values:

- `REALISTIC_WITH_MODELED_CORPORATE_ACTIONS`: raw execution/valuation prices with modeled supported actions.
- `RESEARCH_ONLY_ADJUSTED_PRICES`: adjusted prices are present for execution and the result is not a cash-ledger result.
- `INCOMPLETE_CORPORATE_ACTIONS`: unsupported or incomplete corporate-action modeling affects realism.

## Limitations

Historical performance does not represent future results. Company actions are not fully modeled.
Daily OHLC data cannot prove the intraday order of stop and take-profit triggers, so conservative
same-bar handling is used.
