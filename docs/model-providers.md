# Model providers

## Local OpenAI-compatible endpoints

Configure comma-separated base URLs, typically with the `/v1` suffix. Example candidates are ports previously used for local servers, but none is assumed to be running.

The discovery client probes each endpoint's `/models` route sequentially. It prefers `LOCAL_MODEL_ID` only when that ID is present in the server's result; otherwise it selects the first returned model ID. An inference call goes to `/chat/completions`.

The app does not manage local process lifecycle or download models. It does not infer that a model is good for trading simply because an endpoint answers.

## Hosted fallback

Hosted inference is disabled by default. To enable it, configure `HOSTED_BASE_URL`, `HOSTED_MODEL_ID`, `HOSTED_API_KEY`, and set both `HOSTED_FALLBACK_ENABLED=true` and `HOSTED_FALLBACK_ELIGIBLE=true`. "Eligible" means the operator has verified the current access, free-quota, privacy, rate-limit, and terms-of-use conditions for their own account.

Third-party plans and quotas can change. The software cannot guarantee ongoing free availability. If the local endpoint is missing, hosted fallback is allowed only under this explicit opt-in. It is not automatically used after a timeout from a local inference, because the previous model operation may still be running remotely.

## Prompt and output policy

Model outputs are untrusted. Trade proposals must be machine-parseable and validated against a strict schema. The model cannot place trades, change hard risk controls, or self-promote strategies. Never send API keys, account passwords, private credentials, or unnecessary personal information in prompt payloads.
