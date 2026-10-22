# Intraday Paper Risk Policy

This software is a research and simulation tool, not financial advice. New trade proposals are intraday-only. Legacy positions already in the database may still be reconciled or closed, but new swing entries are rejected.

## Required invariants

- Paper trading is the only supported execution mode.
- The application rejects configuration that enables live order endpoints; broker execution is hard-wired to Alpaca Paper only.
- Manual proposals require explicit approval for the current version. Autonomous entries may open without per-trade approval only after the same deterministic server-side risk checks.
- The backend revalidates the proposal when approving it.
- The proposal's quantity is not trusted; share quantity is calculated by deterministic code.
- A model proposal is saved only if its symbol and timeframe match the request and its entry is within MAX_ENTRY_DEVIATION_PCT of the latest close it was given; otherwise the job fails and nothing is stored.
- Approval freshness is judged against the data timeframe stamped by the server, never the model's own timeframe field. Proposals without that stamp cannot be approved.
- Duplicate approvals and duplicate proposal executions are rejected by an atomic SQLite transaction. Alpaca entry requests also use a stable client order ID so retries can look up an existing broker order instead of submitting another one.
- Stale, malformed, or illogical prices are rejected.
- Strategy changes remain experiments and are never automatically promoted.
- Credentials must never appear in source code, prompts, browser bundles, logs, or Git.
- State-changing API requests require a server-side local bearer token and trusted localhost origin. The token is stored in the ignored root .env and forwarded only by the Next.js server-side proxy.
- When Alpaca Paper execution is enabled, broker/local positions must reconcile before new entries are allowed. Untracked broker positions or legacy local-only positions block new broker entries.

## Initial settings

- Risk budget: 0.25% of the relevant account equity per trade.
- Minimum reward-to-risk: 1.0.
- Maximum entry deviation: 2% from the latest close (MAX_ENTRY_DEVIATION_PCT).
- Per-position allocation cap: MAX_POSITION_VALUE_PCT (default 10% of equity).
- Aggregate gross exposure cap: MAX_GROSS_EXPOSURE_PCT (default 20% of equity).
- Maximum open paper positions: 5.
- Daily loss guard: blocks new entries when current New York calendar day's realized and unrealized P&L reaches the configured threshold. UTC midnight is not the US trading-day boundary.
- Stop loss and take profit are required for a proposal.
- PAPER_INITIAL_EQUITY=100000 applies only to local simulation. In Alpaca Paper mode, account equity, cash, positions and broker marks come from the Alpaca account API.
- These values are engineering defaults, not recommendations for real capital.

## Why the risk budget is $250

At $100,000 equity, a 0.25% risk budget is $100,000 × 0.0025 = $250. That is the estimated loss allowance if the stop is reached, not the amount invested in the shares. Quantity also has to fit the stop distance, the 10% position allocation limit, and the 20% aggregate gross exposure limit. With a $2 entry-to-stop distance, a $250 budget allows at most 125 shares before position/exposure caps and price/slippage effects.

## Quantity

For a long or short position:

- risk per share = absolute value of entry price minus stop-loss price
- risk budget = applicable account equity × risk percentage / 100
- quantity by risk = floor(risk budget / risk per share)

The share quantity is independently constrained by the risk budget, per-position allocation cap and remaining gross exposure. The smallest valid quantity is used; if less than one share fits, the proposal is rejected. The app records the broker-confirmed average fill in Alpaca Paper mode and a computed adverse-slippage fill in local simulation. Market price movement can still make realized execution risk differ from the estimate.

## Execution modes and exits

Local simulation mode writes fake fills and positions to SQLite only; they do not appear in Alpaca. Manual close uses the most recently stored local mark, and OHLC processing simulates stop/target exits only when the app fetches bars.

Alpaca Paper mode submits an equity bracket order containing a market entry, take-profit limit order and stop-loss order. The API endpoint is hard-coded to the paper host. Alpaca Paper positions are reconciled against SQLite; broker-side bracket orders are the authority for exits and manual close is routed through Alpaca Paper with a confirmed fill price. If a broker close or fill cannot be confirmed, the application must not book a stale local mark as the broker's fill.

Alpaca Paper is still a simulation, not a live broker guarantee. It may differ from real execution due to market impact, queue position, slippage, fees, borrow availability and other factors. The session gate excludes weekends and out-of-hours but does not yet use an authoritative holiday/early-close calendar. The app cannot monitor/close positions when it is shut down. Live trading and options are not supported.
