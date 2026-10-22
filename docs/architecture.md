# Architecture

## Runtime

The local application consists of a Next.js dashboard, FastAPI backend, SQLite database, one persistent queue worker, a scheduled market monitor, a model router, and two distinct paper-execution modes:
- **Local simulation** — trade records and fills live only in SQLite.
- **Alpaca Paper** — optional orders route only to the hard-coded https://paper-api.alpaca.markets host through app/execution/alpaca_paper.py. There is no live URL/configuration path.

Browser requests go through the same-origin Next.js proxy. The local bearer token stays server-side and protects state-changing API routes. The backend uses a database-owner lock so two backend processes cannot operate on the same local database concurrently.

## AI and data flow

1. The market monitor checks weekday US regular market hours (default interval: 300 seconds).
2. The Alpaca market-data adapter requests recent bars for the active watchlist and saves normalized observations.
3. Automated entries and user-requested trade proposals are intraday-only and use completed five-minute candles.
4. Open positions use durable bar checkpoints. Local-simulation positions are managed from completed OHLC candles. Alpaca-backed positions rely on Alpaca bracket stop/target orders; the monitor reconciles broker positions/fills and sends end-of-session closes through Alpaca Paper.
5. The queue worker claims one persistent job at a time and invokes the same agent controller. A shared lock limits model generation to one active request.
6. The model's output is untrusted JSON. A proposal must match the requested symbol, the intraday timeframe, and a price close to the latest supplied candle before it can be saved.
7. Risk validation sizes shares from the relevant equity source, stop distance, daily loss, exposure and open-position limits. In Alpaca mode the equity/position exposure inputs are taken from the Alpaca Paper account after reconciliation; in local mode they come from the local SQLite simulation.
8. The local ledger stores execution mode separately for every position. Broker positions that do not reconcile against the local ledger block new Alpaca entries rather than silently blending accounts.
9. A confirmed broker fill is recorded with the Alpaca order ID/average fill price. Order submission uses a stable client order ID to reduce duplicate submissions after retry/restart.

## Account separation

PAPER_INITIAL_EQUITY applies only to local simulation. It does not change the Alpaca account. When Alpaca Paper is enabled, account equity, cash, broker position exposure and unrealized P&L come from the Alpaca account/positions APIs. Legacy SQLite-only positions are shown separately and are excluded from Alpaca equity calculations. Untracked Alpaca holdings are surfaced as reconciliation issues.

## Safety and failure behavior

- Paper-only config is mandatory. The app does not expose or build a live trading endpoint.
- Missing/invalid market-data credentials block fresh scans. Missing/invalid paper trading credentials prevent Alpaca Paper requests.
- A broker entry must pass freshness, deterministic risk checks, and broker reconciliation first. Failed/ambiguous broker requests should be reconciled by client/order ID before retrying.
- Alpaca-backed exits and manual close actions must use the paper Trading API; they are not booked at an old SQLite mark. Entry and close intents are persisted before broker writes. If a write result is ambiguous, the app does not resubmit it blindly; it searches Alpaca orders/positions and blocks new entries until reconciled. Manual retry-release requires a 120-second wait, explicit operator confirmation, and fresh broker-side checks.
- Malformed model output never creates a valid proposal. Hosted inference fallback remains disabled unless explicitly opted in.
- Do not expose the local API publicly and never commit credentials.

## Current scope and limitations

- US-listed equities only; intraday timeframe only for new trade proposals and automated entries.
- Local simulation and Alpaca Paper are separate. Local orders never appear on Alpaca.
- The session gate uses the installed pandas-market-calendars NYSE schedule for holidays and early closes. The schedule is package-shipped, not fetched live, so dependency updates remain important.
- The local simulation is simplified and does not model all fees, partial fills, borrow constraints or broker protections. Alpaca Paper is a simulator and not evidence of live execution quality or profit.
- Keep the app running for monitoring and end-of-session handling; no local service can act while the machine/app is offline.
