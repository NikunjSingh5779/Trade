# Trade — Personal AI Trading Agent

A local-first, single-agent trading research and paper-trading application. Phase 1 targets US equities on Windows, uses a five-minute market scan, discovers configured OpenAI-compatible local model endpoints, and requires explicit approval for every new simulated position.

## Safety status

- Paper trading only. No live order API or live execution adapter is implemented.
- `LIVE_TRADING_ENABLED=false` and `ALLOW_LIVE_ORDER_ENDPOINTS=false` are hard defaults.
- All LLM generation requests use one shared inference gate, backed by an in-process lock and a cross-process file lock.
- Hosted inference is opt-in and requires an explicitly configured eligible endpoint.
- Strategy experiments are never automatically promoted.

## Requirements

- Windows 10/11
- Python 3.12+
- Node.js 20.9+ (for the Next.js dashboard)
- Optional US market-data account and API credentials
- Optional OpenAI-compatible local inference server (for example, llama.cpp or Ollama with an OpenAI-compatible endpoint)

## Start on Windows

Open PowerShell in the repository root.

### 1. Backend

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e .\backend
Copy-Item .env.example .env
# Edit .env if you want local model endpoints or market data.
python -m uvicorn app.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

The API docs are at http://127.0.0.1:8000/docs. Without data credentials, the application still starts; market scans report that the provider is not configured.

### 2. Dashboard

In another PowerShell terminal:

```powershell
cd frontend
npm install
Copy-Item .env.local.example .env.local
npm run dev
```

Open http://127.0.0.1:3000.

### 3. Tests

From the repository root with the virtual environment active:

```powershell
python -m pip install -e .\backend[test]
python -m pytest backend/tests -q
```

## Local model discovery

The backend probes `LOCAL_MODEL_BASE_URLS` sequentially and checks each endpoint's `/models` response. A URL should normally include the OpenAI-compatible API prefix, for example `http://127.0.0.1:8080/v1`. It uses the configured model ID if present and reported by the server; otherwise it chooses the first reported model ID. It will not download, launch, kill, or replace a model server.

Hosted fallback is disabled by default. Set a hosted URL/model/key and explicitly set `HOSTED_FALLBACK_ENABLED=true` and `HOSTED_FALLBACK_ELIGIBLE=true` only after verifying the provider's current free quota, terms, and model access. The application cannot independently guarantee that a third-party quota is free or available.

## Market-data behavior

The initial adapter uses Alpaca's US stock market-data endpoint and the configured feed (default `iex`). The free feed has coverage and entitlement limits. Provide API credentials before enabling scans. Data is fetched for the configured watchlist in one request where the provider supports it. Scans run on a five-minute interval and are restricted to regular US market hours; the first implementation uses weekday/time checks and does not have a full exchange-holiday calendar.

## Safety and limitations

- Model output is untrusted. A proposal must pass Pydantic validation and deterministic risk checks.
- New paper positions require approval bound to a proposal ID and version. The backend revalidates and atomically rejects duplicate approvals.
- Paper fills are simplified simulations at the proposed entry price. They are not evidence of real execution quality.
- The initial version is a foundation, not a profitable-trading guarantee or production-certified financial system.
- Do not expose the local API publicly. This initial local-only dashboard has no multi-user authentication.
- Never put API keys in Git. Use the local `.env` file, which is ignored by Git.

See [docs/architecture.md](docs/architecture.md), [docs/risk-policy.md](docs/risk-policy.md), and [docs/model-providers.md](docs/model-providers.md).
