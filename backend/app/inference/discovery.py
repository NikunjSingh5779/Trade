from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class LocalEndpoint:
    base_url: str
    model_id: str


class EndpointDiscovery:
    def __init__(self, base_urls: list[str], preferred_model: str = "", timeout: float = 3.0):
        self.base_urls = base_urls
        self.preferred_model = preferred_model.strip()
        self.timeout = timeout

    async def discover(self, client: httpx.AsyncClient | None = None) -> list[LocalEndpoint]:
        owned = client is None
        http = client or httpx.AsyncClient(timeout=self.timeout)
        found: list[LocalEndpoint] = []
        try:
            # Intentional sequential probing; this avoids bursts against local servers.
            for base_url in self.base_urls:
                try:
                    response = await http.get(f"{base_url.rstrip('/')}/models")
                    response.raise_for_status()
                    body = response.json()
                    raw_models = body.get("data", []) if isinstance(body, dict) else []
                    model_ids = [
                        item["id"] for item in raw_models
                        if isinstance(item, dict) and isinstance(item.get("id"), str)
                        and item["id"].strip()
                    ]
                    if not model_ids:
                        continue
                    model_id = self.preferred_model if self.preferred_model in model_ids else model_ids[0]
                    found.append(LocalEndpoint(base_url=base_url.rstrip("/"), model_id=model_id))
                except (httpx.HTTPError, ValueError, TypeError, KeyError):
                    continue
            return found
        finally:
            if owned:
                await http.aclose()
