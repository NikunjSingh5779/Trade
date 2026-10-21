# Phase 1 Risk Policy

This software is a research and simulation tool, not financial advice.

## Required invariants

- Paper trading is the only supported execution mode.
- The application rejects configuration that enables live order endpoints.
- Every new paper position requires explicit approval for the current proposal version.
- The backend revalidates the proposal when approving it.
- The proposal's quantity is not trusted; share quantity is calculated by deterministic code.
- A model proposal is saved only if its symbol and timeframe match the request and its entry is within `MAX_ENTRY_DEVIATION_PCT` of the latest close it was given; otherwise the analysis job fails with a `ProposalMismatch` error and nothing is stored.
- Approval freshness is judged against the data timeframe stamped by the server (`data_timeframe`), never the model's own `timeframe` field. Proposals without that stamp cannot be approved and must be regenerated.
- Duplicate approvals and duplicate proposal executions are rejected by an atomic SQLite transaction.
- Stale, malformed, or illogical prices are rejected.
- Strategy changes remain experiments and are never automatically promoted.
- Credentials must never appear in source code, prompts, browser bundles, logs, or Git.

## Initial settings

- Risk budget: 0.25% of simulated equity per trade.
- Minimum reward-to-risk: 1.0.
- Maximum entry deviation: 2% from the latest close (`MAX_ENTRY_DEVIATION_PCT`). At approval, the paper broker rechecks the latest stored bar, rejects an entry that has drifted beyond the configured deviation, and derives the simulated fill from that bar's close with adverse `SIMULATED_SLIPPAGE_BPS` applied.
- Per-position allocation cap: `MAX_POSITION_VALUE_PCT` (default 10% of equity).
- Aggregate gross paper exposure cap: `MAX_GROSS_EXPOSURE_PCT` (default 20% of equity).
- Maximum open paper positions: 5.
- Daily loss guard: blocks new positions when current-day realized paper P&L plus current unrealized paper P&L reaches the configured threshold. Day boundaries are UTC in Phase 1.
- Stop loss and take profit are required for a proposal.
- These values are initial engineering defaults, not recommendations for live capital.

## Quantity

For one share of a long or short position:

`risk_per_share = abs(entry_price - stop_loss)`

`risk_budget = paper_equity * risk_per_trade_pct / 100`

`quantity_by_risk = floor(risk_budget / risk_per_share)`

The share quantity is constrained independently by the risk budget, per-position allocation cap, and remaining aggregate gross exposure. The smallest valid quantity is used; if less than one share fits, the proposal is rejected. Risk is recalculated using the simulated fill price, not the model's requested entry.

## Simulation limitations

Phase 1 simulates an immediate fill at the latest server-stored market-bar close plus adverse entry slippage. A manual close uses the server-recorded position mark; the browser cannot choose the price booked to P&L. Automatic exits process completed OHLC candles from a durable per-position checkpoint, using five-minute bars for intraday positions and completed daily bars for swing positions. When one candle crosses both stop and target without a resolving opening gap, the stop is assumed first. These are still simulations, not broker orders or execution guarantees. Exit checks run only when the application fetches market data; they cannot run while the app is offline. Partial fills, per-exit slippage, fees, borrow availability, short-sale constraints, options and exchange-specific order rules require more work before expanding instrument support.
