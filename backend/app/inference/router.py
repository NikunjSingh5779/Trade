from dataclasses import asdict
from typing import Any

from app.config import Settings
from app.inference.client import OpenAICompatibleClient
from app.inference.discovery import EndpointDiscovery
from app.inference.gate import InferenceGate


class NoModelAvailable(RuntimeError):
    pass


class InferenceRouter:
    def __init__(self, settings: Settings, gate: InferenceGate):
        self.settings = settings
        self.gate = gate
        self.client = OpenAICompatibleClient(
            timeout_seconds=settings.ai_request_timeout_seconds,
            max_output_tokens=settings.ai_max_output_tokens,
            reasoning_budget_tokens=settings.ai_reasoning_budget_tokens,
        )
        self.discovery = EndpointDiscovery(
            settings.local_base_urls,
            settings.local_model_id,
            settings.model_discovery_timeout_seconds,
        )

    async def status(self) -> dict[str, Any]:
        endpoints = await self.discovery.discover()
        eligible_hosted = self.settings.hosted_fallback_enabled and self.settings.hosted_fallback_eligible
        return {
            "local_endpoints": [asdict(endpoint) for endpoint in endpoints],
            "local_available": bool(endpoints),
            "hosted_fallback_enabled": eligible_hosted,
            "selected_mode": "local" if endpoints else ("hosted" if eligible_hosted else "offline"),
            "inference_concurrency_limit": 1,
            # These are the effective settings used to construct this backend's client.
            # They let the dashboard distinguish Trade's request cap from Strata's
            # independent model/context configuration.
            "max_output_tokens": self.settings.ai_max_output_tokens,
            "reasoning_budget_tokens": self.settings.ai_reasoning_budget_tokens,
            "request_timeout_seconds": self.settings.ai_request_timeout_seconds,
        }

    async def complete(self, messages: list[dict[str, str]]) -> tuple[str, dict[str, str]]:
        endpoints = await self.discovery.discover()
        if endpoints:
            # Exactly one chosen endpoint per task; no fan-out and no parallel fallback.
            endpoint = endpoints[0]
            content = await self.gate.run(
                lambda: self.client.complete(endpoint.base_url, endpoint.model_id, messages)
            )
            return content, {
                "provider": "local", "base_url": endpoint.base_url, "model_id": endpoint.model_id
            }

        if self.settings.hosted_fallback_enabled and self.settings.hosted_fallback_eligible:
            content = await self.gate.run(lambda: self.client.complete(
                self.settings.hosted_base_url,
                self.settings.hosted_model_id,
                messages,
                self.settings.hosted_api_key,
            ))
            return content, {
                "provider": "hosted", "base_url": self.settings.hosted_base_url,
                "model_id": self.settings.hosted_model_id,
            }

        raise NoModelAvailable(
            "No compatible local model was discovered and hosted fallback is disabled or ineligible. "
            "Start a configured local model server or explicitly configure an eligible hosted endpoint."
        )
