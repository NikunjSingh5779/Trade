# Model providers

## Local OpenAI-compatible endpoints

Configure comma-separated base URLs, typically with the /v1 suffix. Example candidates are ports previously used for local servers, but none is assumed to be running.

The discovery client probes each endpoint's /models route sequentially. It prefers LOCAL_MODEL_ID only when that ID is present in the server's result; otherwise it selects the first returned model ID. An inference call goes to /chat/completions.

The app does not manage local process lifecycle or download models. It does not infer that a model is good for trading simply because an endpoint answers.

## Hosted fallback

Hosted inference is disabled by default. To enable it, configure HOSTED_BASE_URL, HOSTED_MODEL_ID, HOSTED_API_KEY, and set both HOSTED_FALLBACK_ENABLED=true and HOSTED_FALLBACK_ELIGIBLE=true. "Eligible" means the operator has verified the current access, free-quota, privacy, rate-limit, and terms-of-use conditions for their own account.

Third-party plans and quotas can change. The software cannot guarantee ongoing free availability. If the local endpoint is missing, hosted fallback is allowed only under this explicit opt-in. It is not automatically used after a timeout from a local inference, because the previous model operation may still be running remotely.

## Prompt and output policy

Model outputs are untrusted. New trade proposals must be intraday-only, machine-parseable, and validated against the strict proposal schema. The model cannot place trades, change hard risk controls, or self-promote strategies. Never send API keys, account passwords, private credentials, or unnecessary personal information in prompt payloads.

## Execution and account separation

AI inference and order execution are separate systems. By default, the app only writes local simulated positions to SQLite; those positions never appear in Alpaca. Optional broker execution is a separate, paper-only adapter pointed at https://paper-api.alpaca.markets. Configure ALPACA_PAPER_TRADING_ENABLED=true, ALPACA_PAPER_API_KEY, and ALPACA_PAPER_API_SECRET in the root .env to opt in. Use paper keys, not live keys. There is no live-order endpoint or configurable live host. Existing local-simulation positions must be reconciled/closed before Alpaca entries are permitted.

New entries use completed five-minute candles and are intraday-only. An AI answer by itself is not an executed order; use the execution mode and broker reconciliation status to verify an order. Keep market-data credentials (MARKET_DATA_API_KEY and MARKET_DATA_API_SECRET) separate in configuration from paper Trading API credentials.

## News and economic events

Optional RSS/Atom feeds can be configured via NEWS_RSS_URLS and ECONOMIC_EVENTS_RSS_URLS in the local .env file. URLs must use HTTPS. Feeds are fetched sequentially, deduplicated, and passed as sourced items in market-scan/proposal context. No feed URL is accepted from model output. Empty configuration means news/events are unavailable; the app does not claim comprehensive event-calendar coverage.
