# IRON_CONDOR_V1

A fixed four-leg NIFTY iron condor on Zerodha Kite Connect. This document maps
each specification item to the implementation, defines the numbers the
strategy acts on, and covers shadow trading, backtesting and the live-trading
gate.

These are strategy parameters to implement and evaluate. Nothing here implies
the strategy is profitable.

## Specification → implementation

| # | Requirement | Where / how |
|---|---|---|
| 1 | NIFTY 50 options via Kite Connect | `brokers/kite.py` `KiteDataSource` (instruments, quotes, funds, basket margin). Underlying quote `NSE:NIFTY 50`. |
| 2 | One attempt at 10:00 IST, ≤ 60 s scheduler delay, else skip; no later re-entry | `session.py` `run_day`: waits until 10:00, skips with `ENTRY_WINDOW_MISSED` if more than 60 s late. `entry_attempted` is persisted **before** any entry work (`state.py`), so restarts cannot retry. |
| 3 | Nearest expiry with ≥ 10 calendar days (configurable) | `IronCondorV1.select_expiry`, `[strategy] min_days_to_expiry`. |
| 4 | Legs at \|Δ\| ≈ 0.05 / 0.25 / 0.25 / 0.05, same underlying and expiry, compatible lots, strict strike order, bounded risk | `_chain` derives IV and delta (Black-76, parity-implied forward) from live quotes; `_select_legs` picks the closest strike within a tolerance; `_validate_structure` checks expiry, underlying, lot size, `LP < SP < SC < LC`, positive credit and finite max loss. Equal quantities on all four legs bound the loss at expiry. |
| 5 | Size from broker-confirmed funds and full-basket margin, ≤ 90%, whole units | `_size`: `kite.margins("equity")["net"]` × 0.90 is the cap. Margin for one basket from `basket_order_margins` gives the starting unit count, then the **full** basket margin is re-queried and units are reduced until it fits. |
| 6 | Target +0.25% of approved basket margin | `exit_signal`, `SPEC.profit_target_fraction`. |
| 7 | Stop −0.50% of approved basket margin | `exit_signal`, `SPEC.stop_loss_fraction`. |
| 8 | Time exit 15:25 IST | `session._monitor` and `exit_signal`. |
| 9 | Protective legs first; shorts only against confirmed protective fills; submission ≠ execution | `execution.BasketExecutor.enter`: buys both wings, waits for final order status **and** matching broker positions, then sells shorts for `min(confirmed long put, confirmed long call)` complete lots only. |
| 10 | Partial fills / failures: trim or unwind from broker-reconciled exposure, no blind retries, flat before closed, manual intervention | See *Execution safety* below. |
| 11 | One logical basket per day; no re-entry after any exit | Persisted day state; after a skip, abort or exit, `run_day` returns `ALREADY_ATTEMPTED`. |
| 12 | Charges, taxes, slippage applied consistently; real fills live | `costs.py` charges per executed order; `pnl.py` net P&L = gross MTM − charges paid − estimated exit charges. Live P&L uses broker order fills (average price and filled quantity from order history). |
| 13 | Shadow mode: real Kite data, simulated orders, full records, no real orders | `mode = "shadow"`: `KiteDataSource` over `ReadOnlyKite` (order endpoints unreachable) + `SimulatedOrderGateway`. Everything is journalled to `runs/shadow/journal/*.jsonl`. |
| 14 | Live disabled until verified and authorised; nothing can bypass | `safety.py` `LiveTradingGuard`, enforced inside `KiteOrderGateway` on construction and before every order or cancel. |
| 15 | V1 first, compatible with the broader system | `strategies/base.py` `Strategy` protocol + `STRATEGIES` registry. Session, executor, journal and brokers are strategy-agnostic. |

The fixed rules (deltas, times, 90%, +0.25%, −0.50%) are constants in
`strategies/iron_condor_v1.py` `SPEC` and cannot be overridden by
configuration. Changing them means defining a new strategy, so "IRON_CONDOR_V1"
always means the same rules in journals and reports.

## Definitions

- **Approved basket margin basis**: broker basket margin (`final.total` from
  Kite's basket margin API) for the basket at the size actually entered. If a
  partial fill trims the basket to fewer units, the margin is re-queried for
  the trimmed size.
- **Net basket P&L**: premium cash flow + open quantity × mark − charges paid −
  estimated charges to close. With `mark_method = "exit_side"` (default),
  longs are marked at bid and shorts at ask: what closing would actually get.
- **Delta**: Black-76 on the forward implied by put-call parity at the three
  strikes nearest spot, with IV solved from each strike's mid price. Reported
  as spot-equivalent Black-Scholes delta (`N(d1)` for calls, `N(d1) − 1` for puts).

## Costs relative to the target and stop

The target and stop are small. Charges are flat per order (brokerage and GST)
plus turnover-based (STT, exchange, SEBI, stamp). With illustrative prices
(about ₹50,000 margin per lot):

| Lots | Target | Stop | Round-trip charges (8 orders) |
|---|---|---|---|
| 1 | ₹125 | ₹250 | ~₹227 + spread |
| 5 | ₹625 | ₹1,250 | ~₹381 + spread |
| 18 | ₹2,250 | ₹4,500 | ~₹880 + spread |

Because net P&L includes estimated exit charges from the moment of entry, a
1-lot basket starts close to its stop. Each entry plan journals
`estimated_friction` and `friction_fraction_of_stop` so this is visible every
day.

## Execution safety

- Orders are marketable **limit** orders: the touch price moved
  `limit_buffer_pct` through the market. Each order gets a unique tag.
- An order counts as filled only once its final status (COMPLETE, or CANCELLED
  with a partial fill) is read from the broker. Exposure is cross-checked
  against broker positions (`_sync_exposure`), allowing for positions to lag.
- **Unknown placement outcome** (network error or timeout): the order is looked
  up by its tag, never resubmitted. If it can't be found, that leg is
  *blocked*: no more orders are sent for it, its wing is kept, and the basket is
  halted for manual intervention.
- **Partial fills**: units = the smallest broker-confirmed fill across the four
  legs, in whole lots. Any excess is trimmed. Shorts are bought back before
  wings are sold, and a wing is never sold below the open quantity of its short
  leg (using whichever of ledger and broker shows more short).
- **Unwind and trim quantities** come from the fill ledger and are capped per
  leg by the original excess, counted independently from order fills. A stale
  positions feed or an inconsistent ledger therefore cannot cause repeated
  orders.
- **CLOSED / ABORTED** are set only once broker positions for all four
  contracts are confirmed flat and agree with the ledger. Otherwise the basket
  and the day become **HALTED**: a CRITICAL alert goes out (log,
  `runs/<mode>/alerts.jsonl`, and the webhook if `OSS_ALERT_WEBHOOK_URL` is set),
  and automation does nothing further that day.
- The entry is skipped if any selected contract already has a position in the
  account, so reconciliation is unambiguous.
- Restarts: an OPEN basket is resumed (monitored, then closed). An entry
  interrupted mid-way is halted for manual review, never retried.
- The `NRML` product is the default, to avoid racing Zerodha's MIS auto
  square-off around the 15:25 exit.

## Shadow trading (daily)

```bash
pip install -e '.[kite]'
cp config/iron_condor_v1.example.toml config/iron_condor_v1.local.toml   # mode = "shadow"
export KITE_API_KEY=... KITE_API_SECRET=...
oss kite-login --config config/iron_condor_v1.local.toml     # once per day, after Kite login
oss run --config config/iron_condor_v1.local.toml             # start before 10:00 IST; returns after exit
```

Example cron entry (machine clock in IST): `45 9 * * 1-5 cd /srv/oss && oss run --config config/iron_condor_v1.local.toml`.
Add NSE holidays to `[strategy] holidays`.

The journal (`runs/shadow/journal/IRON_CONDOR_V1_<date>.jsonl`) records:
`entry_attempt`, `expiry_selected`, `chain_snapshot` (quotes, IV, delta per
strike), `sizing`, `entry_plan` (legs, Greeks, quotes, margin, credit, max
loss, friction), `order_submit` / `order_final` (simulated fills),
`basket_entered`, `mtm` (each minute), `exit_signal`, `basket_closed` (gross,
charges, net), and every skip, exception and alert.

Shadow sizing uses the account's real funds and real basket margin from Kite
(read-only calls). Shadow fills walk the quoted market depth (`fill_model = "depth"`).

## Backtesting

```bash
oss backtest --config config/iron_condor_v1.local.toml --data /path/to/data --start 2026-04-17 --end 2026-10-16
```

The backtest replays every day in the data through the **same** session,
strategy and executor code, with a simulated clock and simulated fills. Output:
`days.csv` (one row per day: outcome, reason, strikes, deltas, units, margin,
exit reason, gross, charges, net), `summary.json`, `summary.md`, and a journal
per day.

### Data required

Kite's historical API does not serve expired option contracts, so six months
of NIFTY option history must come from a data vendor, or from your own
recordings (shadow-mode journals capture chains going forward). Per trading day:

```
<data>/underlying/YYYY-MM-DD.csv   timestamp,price
<data>/options/YYYY-MM-DD.csv      timestamp,expiry,strike,option_type,close[,bid,ask,lot_size]
```

- Timestamps in IST. Include every expiry at least out to `min_days_to_expiry`
  + 10 days, and strikes at least ±10% around spot.
- `bar_timestamp = "start"` (vendor norm): a row stamped 10:00 is the
  10:00–10:01 bar and becomes usable at 10:01. This avoids look-ahead.
- Without bid/ask columns, a `synthetic_spread_pct` spread is built around the close.

### Backtest limitations (read before trusting numbers)

- **Margin is approximate**: Kite's margin API can't price past dates, so the
  margin is `widest wing × qty × margin_wing_factor + margin_exposure_pct × spot × qty`.
  Because the target and stop are percentages of margin, calibrate this against
  Zerodha's margin calculator for a few real baskets before relying on results.
- Minute bars: target and stop are checked on bar closes, so moves within a
  minute are missed. Fills are at the touch price ± `slippage_ticks`.
- `oss synthetic-data` writes a **synthetic** dataset only to exercise the
  pipeline. Reports built from it are marked as synthetic and say nothing
  about the strategy.

## Enabling live trading (not yet authorised)

Live stays blocked unless **all** of these hold (`oss live-status` lists what is missing):

1. `[live] enabled = true`;
2. `config/live_verification.json` (template: `config/live_verification.example.json`)
   names `IRON_CONDOR_V1`, records every required safety check as passed with
   who verified it and when, and carries an explicit authorization
   (`authorized_by`, `authorized_on`, `valid_until`, `authorization_code`);
3. environment variable `OSS_LIVE_AUTHORIZATION` equals that `authorization_code`.

The guard runs before every live order and cancel, so withdrawing the
environment variable or letting `valid_until` pass stops order flow
mid-session. There is no override flag.
