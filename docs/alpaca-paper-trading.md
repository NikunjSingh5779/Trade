# Alpaca Paper Trading Setup and Troubleshooting

This guide applies to the optional Alpaca Paper execution mode in Trade. Read it before placing a simulated order.

## The important distinction

- **Local simulation:** the app writes a fake fill into its own SQLite database. Alpaca will not display it.
- **Alpaca Paper:** the app submits an order to Alpaca's simulated brokerage API; Alpaca displays its order, position and paper-account activity.
- **Live brokerage:** unsupported. This application has no live trading URL or live order adapter.

Alpaca's Paper Trading API uses the base URL https://paper-api.alpaca.markets and different credentials from the live API. The code fixes its order adapter to that paper host rather than allowing an arbitrary URL.

## How the $100,000 amount works

Alpaca documentation says a new Paper Trading account starts with $100,000 by default. The dashboard now supports creating/deleting paper accounts; to get a clean account with the default balance, create a new Paper account in the Alpaca dashboard and generate keys for that new account. A paper account's balance is not controlled by this project's .env.

PAPER_INITIAL_EQUITY=100000 is separate and only initializes the app's local simulation. When Alpaca Paper mode is active and connected, Trade reads equity/cash from the Alpaca account API.

$250 comes from the local default RISK_PER_TRADE_PCT=0.25: $100,000 × 0.0025 = $250 estimated stop-loss risk. It is not the order's purchase value. For example, if the fill is $100 and stop is $98, each share has roughly $2 planned risk, so the $250 risk cap allows 125 shares before other controls; the 10% single-position allocation cap then limits a $100,000 account to about $10,000 notional, or around 100 shares at $100.

## One-time setup

1. Sign in to Alpaca and select/create a **Paper Trading** account.
2. Generate the **paper** API key and secret. Do not use or paste live keys.
3. In the repository root .env, configure:
   - ALPACA_PAPER_TRADING_ENABLED=true
   - ALPACA_PAPER_API_KEY=your paper key ID
   - ALPACA_PAPER_API_SECRET=your paper secret
   - MARKET_DATA_API_KEY and MARKET_DATA_API_SECRET for market-data requests.
4. Keep PAPER_TRADING_ONLY=true, LIVE_TRADING_ENABLED=false and ALLOW_LIVE_ORDER_ENDPOINTS=false.
5. Restart scripts/dev.ps1.
6. On the dashboard, check **Alpaca Paper API connected** and **Broker reconciliation: Matched**. Confirm the reported equity against Alpaca's paper dashboard before enabling autonomous entries.

Do not paste credentials in chat, screenshots, issues, logs or source control.

## What happens to existing positions?

Positions created before broker mode was enabled are marked local_simulation by the database migration. They are not imported into Alpaca, even if they have familiar tickers, share counts or prices. The dashboard separates them and explicitly labels them LOCAL ONLY. Review their symbol, side, quantity, entry, mark, stop and target.

**Recommended clean test without deleting your existing three local rows:** stop the app, back up the root .env and current SQLite database, then set DATABASE_PATH to a new file such as data/alpaca_paper.db. The app creates a clean database on restart; the old local-only positions remain in the original file and are not imported or closed. When you are ready to use one database instead, Close at local mark only closes a legacy SQLite simulation at its recorded mark; it does not place an Alpaca order. The app blocks new Alpaca entries while local-only positions are open, or if Alpaca has holdings the local database does not track, so it cannot silently mix different account states.

An Alpaca-backed position is different: the app sends a close through Alpaca Paper and records the confirmed broker fill. Its stop and target are Alpaca bracket-order legs; edit those from Alpaca's Paper dashboard. If a close request times out, the app stores a durable close intent and will not send a second close automatically. After at least 120 seconds, check the Alpaca Paper Orders page and Positions page. The dashboard only exposes a retry-release control after you explicitly confirm there is no matching close order and the broker position remains open; it also re-checks both conditions through the Paper API. If broker status is unavailable or reconciliation is not matched, stop opening new trades and investigate before retrying orders.

## Intraday policy

New entries and trade proposals are intraday-only. They use completed five-minute candles and are only eligible during the NYSE regular session. All new approvals are blocked during the final ten minutes before the scheduled close. The target/stop bracket is sent with the Alpaca parent entry order. The monitor polls every 30 seconds during that final window and targets Alpaca-backed closes five minutes before the scheduled close; local-only simulations are marked closed at the scheduled close. This is best-effort, not a guarantee if Alpaca is unavailable or Trade is offline. Keep Trade running during the session: a local process cannot act while your PC/application is offline.

The session gate uses the installed NYSE calendar package to include holidays and early closes. Its calendar is shipped with the installed dependency, not queried live; keep it updated. Do not rely on this app as unattended production trading software.

## Useful checks

- Dashboard: http://localhost:3000
- API docs: http://127.0.0.1:8000/docs
- Read-only execution status: GET /api/execution/status
- Portfolio and separated account values: GET /api/portfolio
- Alpaca's account and order state: the Alpaca Paper dashboard

Alpaca Paper is only a simulation. Fill models, slippage, queues, partial fills, fees, dividends and other conditions can differ from live trading. This project must never be treated as evidence of profitability.
