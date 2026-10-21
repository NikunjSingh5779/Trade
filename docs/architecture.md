# Architecture

## Runtime

The local application consists of a Next.js dashboard, a FastAPI backend, a SQLite database, one queue worker, one scheduled market monitor, and a model router. It is designed for use on the Windows machine's loopback interface. Browser requests go through a same-origin Next.js proxy; the backend's high-entropy local bearer token stays on the server and is required for state-changing API requests. The `scripts/dev.ps1` launcher generates the token once in the ignored root `.env` file if absent.

## Single-agent rule

There is one AI controller. Task roles (chat, scan analysis, proposal research) are prompts sent sequentially to one selected model. No agent fan-out or model ensemble is used.

Every text-generation call is passed through `InferenceGate`. It uses an `asyncio.Lock` for tasks in the current process and a `portalocker` lock file for cooperating backend processes. A second backend process pointed at the same database is rejected by a database-owner lock. Local endpoint discovery only performs a `GET /models`; it is not generation. The router selects one local endpoint. Hosted fallback may be used only when no local endpoint is discovered and the user explicitly configures an eligible provider.

A client timeout can leave ambiguity about whether a remote server continues generating after a disconnected client. The app does not retry or fall back automatically after generation errors. If a model server's generation state is ambiguous, stop and inspect/reset that server before resuming. File locks coordinate this app's processes; they cannot control unrelated clients using the same model server.

## Data path

1. The market monitor checks weekday US regular market hours every configured interval (default 300 seconds).
2. The market adapter requests recent bars for the active watchlist and persists normalized observations.
3. Open positions have a durable last-processed-bar checkpoint. The monitor backfills from each open position's entry/checkpoint timestamp using provider pagination, processes completed OHLC candles chronologically, and records paper exits. Intraday positions use five-minute candles; swing positions use completed daily candles.
4. The monitor coalesces market-scan jobs by scan window and skips AI enqueuing if the queue is full, without skipping data collection.
5. The queue worker claims one persistent job at a time and invokes the same agent controller.
6. Proposal analysis only persists an idea if the model produces valid JSON matching the strict proposal schema, the requested symbol and timeframe, and an entry price near the latest close. The server stamps the data timeframe on the proposal, and approval freshness uses that stamp rather than the model's own label.
7. Risk validation sizes shares using equity, stop distance, risk budget, daily loss, gross exposure, and max-open-position rules.
8. A human approval is bound to proposal ID and version.
9. The SQLite transaction inserts a simulated order and position exactly once, and marks the proposal approved. Paper fills use the latest server-stored bar close with adverse simulated entry slippage; the proposed entry itself is not assumed to have traded.

## Failure behavior

Malformed model outputs do not create proposals. Missing models fail AI jobs with an actionable error. Missing data credentials prevent market retrieval. A restart changes jobs marked running into failed for inspection instead of replaying them without context; queued jobs remain queued. Errors are bounded before they are displayed.

## Phase 1 exclusions

- No live order placement or live broker adapter
- No automatic strategy promotion
- No multi-agent execution or parallel inference
- No public hosting or multi-user authentication
- No guarantees about provider free quotas or simulated fill fidelity
- Initial session check excludes weekends and outside-hours but is not an exchange holiday calendar
- Paper fills remain simplified; partial fills, full entry/exit slippage modeling, fees, borrow costs, and broker-side protective orders are not implemented
