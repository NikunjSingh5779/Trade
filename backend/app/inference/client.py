from typing import Any

import httpx


class OpenAICompatibleClient:
    def __init__(
        self,
        timeout_seconds: float = 300,
        max_output_tokens: int = 8192,
        reasoning_budget_tokens: int = 4096,
    ):
        if max_output_tokens < 1024:
            raise ValueError("max_output_tokens must be at least 1024.")
        if reasoning_budget_tokens < 0 or reasoning_budget_tokens >= max_output_tokens:
            raise ValueError("reasoning_budget_tokens must leave room for the final answer.")
        self.timeout_seconds = timeout_seconds
        self.max_output_tokens = max_output_tokens
        self.reasoning_budget_tokens = reasoning_budget_tokens

    async def complete(
        self, base_url: str, model_id: str, messages: list[dict[str, str]], api_key: str = ""
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": 0.2,
            # Reasoning and final text share this output ceiling. A separate hard
            # reasoning cap prevents long analysis from consuming the full answer budget.
            "max_tokens": self.max_output_tokens,
            "reasoning_budget_tokens": self.reasoning_budget_tokens,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
            data = response.json()

        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(
                "Model response did not contain choices[0].message."
            ) from exc

        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            # Some OpenAI-compatible servers return text as content blocks.
            text = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            ).strip()
        else:
            text = ""

        if not text:
            finish_reason = choice.get("finish_reason", "unknown") if isinstance(choice, dict) else "unknown"
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            completion_tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
            message_fields = sorted(
                key for key in message
                if key != "content"
            ) if isinstance(message, dict) else []
            details = (
                "Model returned no final text "
                f"(finish_reason={finish_reason}, completion_tokens={completion_tokens}, "
                f"message_fields={message_fields})."
            )
            if finish_reason == "length" or "reasoning_content" in message_fields:
                details += (
                    " The local server may be exhausting its output budget in reasoning; "
                    "check its token/context settings or disable thinking mode if supported."
                )
            raise ValueError(details)
        return text
