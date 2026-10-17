"""Option Selling System.

A modular framework for rule-based index option selling strategies on Zerodha
Kite Connect. The first (and currently only) strategy is IRON_CONDOR_V1.

Layers, from the bottom up:

* ``models`` / ``basket``   - domain types and basket accounting
* ``greeks`` / ``costs``     - pricing, implied volatility, delta, Zerodha charges
* ``brokers``                - market/account data sources and order gateways
                               (Kite read-only data, simulated fills, guarded live orders)
* ``execution``              - broker-reconciled basket entry, trimming, unwinding, exit
* ``strategies``             - strategy rules (entry plan, sizing, exit signals)
* ``session``                - one trading day: schedule, entry, monitoring, exit
* ``backtest``               - historical replay through the same session code
"""

__version__ = "0.1.0"
