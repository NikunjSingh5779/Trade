# Trade — Personal AI Intraday Trading Agent

A local-first US-equities intraday research and paper-trading application for Windows. It analyzes completed five-minute candles, asks one configured local AI model for a Long/Short/No Trade decision, applies deterministic risk checks, and closes intraday broker-backed positions through Alpaca Paper at the end of the regular session.

## Execution modes — read this before trading

The default mode is **local simulation**. It writes simulated fills to the app's SQLite database. Those trades will never appear in Alpaca.

Optional **Alpaca Paper mode** sends orders to the hard-coded https://paper-api.alpaca.markets endpoint. It records confirmed Alpaca Paper fills in SQLite, requests bracket orders with a stop and target, and displays the Alpaca account's equity/cash instead of presenting local simulation figures as broker figures. Live order endpoints are not implemented or configurable.

Existing local-simulation positions cannot be transferred to Alpaca. To preserve them while testing broker execution, back up your current database and set DATABASE_PATH to a new file such as data/alpaca_paper.db before enabling Alpaca Paper; that clean database keeps old local rows intact in their original file. If you reuse the original database instead, local-only open positions must be reviewed/closed before broker entries can proceed. The app flags local/broker discrepancies and blocks new Alpaca entries until reconciliation succeeds. Entry and close intents are persisted before broker writes; ambiguous outcomes are never blindly retried. A dashboard recovery control requires a 120-second wait, explicit operator confirmation and fresh checks against Alpaca Paper orders/positions.

## Safety settings

- PAPER_TRADING_ONLY=true
- LIVE_TRADING_ENABLED=false
- ALLOW_LIVE_ORDER_ENDPOINTS=false
- ALPACA_PAPER_TRADING_ENABLED=false by default; opt in only after configuring paper keys and reviewing existing positions.
- Automated new positions are intraday-only and only allowed during regular US market hours. Swing entry requests are rejected.
- New automated entries must pass server-side freshness, price-consistency, size, stop/target, daily-loss, position-count and exposure checks.
- Alpaca Paper orders use an idempotent client order ID and a bracket stop/target. Alpaca remains a simulator, not real-money execution or proof of profitability.
- Session checks use the installed NYSE calendar package, including holidays and scheduled early closes. New approvals are blocked outside regular hours and during the final ten minutes before the scheduled close. The monitor polls every 30 seconds during the final ten minutes and targets Alpaca Paper closes five minutes before the scheduled close. Keep the app running; a local monitor cannot act while it is offline.
- Do not commit or paste API credentials. Store separate Alpaca Paper credentials in the ignored root .env only.

## Requirements

- Windows 10/11
- Python 3.12+
- Node.js 20.9+
- Alpaca account with API access for US stock market data
- Optional OpenAI-compatible local inference server, such as llama.cpp or Ollama

## Start on Windows

Open PowerShell in the repository root. The launcher creates .env and a local API token if needed.

```powershell
.\scripts\dev.ps1
```

The API documentation is at http://127.0.0.1:8000/docs and the dashboard is at http://127.0.0.1:3000. Stop the app using Ctrl+C in the backend and frontend terminals.

## Configure market data

Set MARKET_DATA_API_KEY and MARKET_DATA_API_SECRET in the root .env. MARKET_DATA_FEED=iex is the default. A valid market-data feed/API key is required for scans. Never print or share key values.

## Connect Alpaca Paper order execution

1. In the Alpaca dashboard, open/select a **Paper Trading** account and generate its paper API key pair. Paper keys are different from live keys.
2. Make sure you can read the account at the Paper API host and confirm that the paper account has the balance you expect. New Alpaca Paper accounts default to $100,000; create a new paper account if you need a clean default account.
3. Review/close any legacy local-simulation positions in the dashboard first. In local mode their close action only closes the SQLite simulation; they are not Alpaca holdings.
4. Put the paper credentials into .env:
   - ALPACA_PAPER_TRADING_ENABLED=true
   - ALPACA_PAPER_API_KEY= (paper key ID)
   - ALPACA_PAPER_API_SECRET= (paper secret)
   - Keep market-data credentials under MARKET_DATA_API_KEY and MARKET_DATA_API_SECRET.
5. Restart .\scripts\dev.ps1. Verify that the dashboard reports **Alpaca Paper API connected** and **Broker reconciliation: Matched** before expecting orders in Alpaca. Do not enable broker routing if the dashboard reports discrepancies.

See docs/alpaca-paper-trading.md for the broker workflow and troubleshooting.

## What $100,000 and $250 mean

- PAPER_INITIAL_EQUITY=100000 is starting equity **only for local simulation**.
- Alpaca Paper equity is fetched from Alpaca's account API; changing the local setting does not fund or reset Alpaca.
- RISK_PER_TRADE_PCT=0.25 means a $250 stop-loss risk budget on $100,000 equity (100000 × 0.0025). It is not the amount invested in the trade. Quantity is also limited by stop distance, the 10% per-position cap and the 20% gross-exposure cap.
- The default 1% daily loss limit is $1,000 on $100,000 equity. These are simulation safeguards, not financial advice.

## Tests

```powershell
if (-not (Test-Path ".\.venv\Scripts\python.exe")) { py -3.12 -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -e ".\backend[test]"
.\.venv\Scripts\python.exe -m pytest backend/tests -q
```

Frontend checks:

```powershell
Set-Location .\frontend
npm install
npm run lint
npm run build
```

## Important limitations

Paper results do not establish profitability. Alpaca Paper simulates fills; this application does not guarantee the same fills, account state or market outcomes as live trading. The session checker now uses the installed NYSE calendar package for holidays and early closes. Its schedule is shipped with the package rather than fetched live, so update the dependency regularly. Broker disconnect/restart/reconciliation paths should be tested before relying on it unattended. Live trading is intentionally unsupported.
