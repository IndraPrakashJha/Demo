# Option Selling System

Rule-based NIFTY option selling on Zerodha Kite Connect. The first strategy is
**IRON_CONDOR_V1** (spec, implementation map and operating guide:
[docs/IRON_CONDOR_V1.md](docs/IRON_CONDOR_V1.md)).

| Mode | Market data | Orders | Status |
|---|---|---|---|
| `shadow` | genuine Kite data (read-only client) | simulated fills, journalled | ready to run |
| `backtest` | historical minute data you supply | simulated fills, same session code | ready; needs data |
| `live` | Kite | real Kite orders | **blocked** until verified and authorised |

## Quick start

```bash
pip install -e '.[kite,dev]'
pytest                                   # 75 tests, no network needed

# Pipeline smoke test on SYNTHETIC data (not market data)
oss synthetic-data --out runs/synthetic --start 2026-04-20 --days 5
oss backtest --config config/iron_condor_v1.example.toml --data runs/synthetic \
    --start 2026-04-20 --end 2026-04-24

# Shadow trading with real Kite data
export KITE_API_KEY=... KITE_API_SECRET=...
oss kite-login --config config/iron_condor_v1.example.toml
oss run --config config/iron_condor_v1.example.toml
```

## Layout

```
src/option_selling/
  strategies/iron_condor_v1.py  fixed V1 rules: expiry, delta strikes, sizing, exits
  execution.py                  broker-reconciled entry / trim / unwind / exit
  session.py                    one trading day: schedule, single attempt, monitor, exit
  brokers/kite.py               Kite read-only data + guarded live order gateway
  brokers/simulated.py          hypothetical fills (shadow, backtest)
  safety.py                     live-trading guard
  greeks.py, costs.py, pnl.py   IV/delta, Zerodha charges, net MTM
  journal.py, state.py, alerts.py
  backtest/                     historical data, replay engine, reports, synthetic data
config/                         example config and live sign-off template
tests/                          unit and end-to-end tests
```
