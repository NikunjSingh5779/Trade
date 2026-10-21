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
- Maximum entry deviation: 2% from the latest close (`MAX_ENTRY_DEVIATION_PCT`). The paper broker fills at the proposed entry, so an entry the market has not reached would book fictitious P&L.
- Maximum open paper positions: 5.
- Gross paper exposure cap: 20% of simulated equity.
- Daily loss guard: blocks new positions when current-day realized paper P&L plus current unrealized paper P&L reaches the configured threshold. Day boundaries are UTC in Phase 1.
- Stop loss and take profit are required for a proposal.
- These values are initial engineering defaults, not recommendations for live capital.

## Quantity

For one share of a long or short position:

`risk_per_share = abs(entry_price - stop_loss)`

`risk_budget = paper_equity * risk_per_trade_pct / 100`

`quantity_by_risk = floor(risk_budget / risk_per_share)`

The share quantity is also constrained by remaining allowed gross exposure. The smaller valid quantity is used; if less than one share fits, the proposal is rejected.

## Simulation limitations

Phase 1 fills at the proposed entry price and allows a user to close a simulated position at an explicitly supplied simulation price. These are not realistic execution guarantees. Partial fills, market gaps, borrow availability, short-sale rules, options, contract multipliers, taxes, fees and slippage require additional work before expanding instrument support.
