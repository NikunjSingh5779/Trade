from typing import Any

import httpx


class OpenAICompatibleClient:
    def __init__(self, timeout_seconds: float = 180):
        self.timeout_seconds = timeout_seconds

    async def complete(
        self, base_url: str, model_id: str, messages: list[dict[str, str]], api_key: str = ""
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: dict[str, Any] = {
            "model": model_id, "messages": messages, "temperature": 0.2, "max_tokens": 1200,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
            data = response.json()
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Model response did not contain choices[0].message.content.") from exc
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Model returned an empty response.")
        return content.strip()
