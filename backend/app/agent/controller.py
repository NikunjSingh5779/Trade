import json
from typing import Any

from app.agent.prompts import build_messages
from app.inference.router import InferenceRouter


class AgentController:
    """A single model-driven agent with sequential task modes and no tool fan-out."""

    def __init__(self, router: InferenceRouter):
        self.router = router

    async def run(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        messages = build_messages(task, payload)
        text, provider = await self.router.complete(messages)
        result: dict[str, Any] = {
            "task": task,
            "text": text,
            "provider": provider,
        }
        if task == "analyze_proposal" or (task == "market_scan" and payload.get("autonomous_mode") is True):
            try:
                result["structured"] = json.loads(text)
            except json.JSONDecodeError:
                result["structured"] = None
                result["parse_warning"] = "Model output was not valid JSON; no automated paper trade was created."
        return result
