import asyncio
from typing import Any

from app.agent.controller import AgentController
from app.agent.schemas import TradeProposal
from app.inference.gate import InferenceGateBusy
from app.market_data.freshness import ensure_fresh_bars
from app.storage import Database, utc_now


class QueueWorker:
    def __init__(
        self, db: Database, agent: AgentController, max_pending: int = 100,
        max_stale_data_seconds: int = 600,
    ):
        self.db = db
        self.agent = agent
        self.max_pending = max_pending
        self.max_stale_data_seconds = max_stale_data_seconds
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            job = self.db.claim_next_job()
            if not job:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                except TimeoutError:
                    pass
                continue
            try:
                result = await self._process(job["task"], job["payload"])
                self.db.finish_job(job["id"], result)
                self.db.add_audit("job.succeeded", {"job_id": job["id"], "task": job["task"]})
            except InferenceGateBusy:
                with self.db.connect() as conn:
                    conn.execute(
                        "UPDATE jobs SET status='queued',updated_at=? WHERE id=?",
                        (utc_now(), job["id"]),
                    )
                await asyncio.sleep(0.5)
            except Exception as exc:
                message = str(exc).replace("\n", " ")[:400]
                self.db.fail_job(job["id"], f"{type(exc).__name__}: {message}")
                self.db.add_audit("job.failed", {
                    "job_id": job["id"], "task": job["task"], "error_type": type(exc).__name__,
                })

    def stop(self) -> None:
        self._stop.set()

    async def _process(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
        if task == "analyze_proposal":
            ensure_fresh_bars(
                payload.get("bars", []),
                str(payload.get("timeframe", "intraday")),
                self.max_stale_data_seconds,
            )
            result = await self.agent.run(task, payload)
            structured = result.get("structured")
            if isinstance(structured, dict) and not structured.get("no_setup"):
                proposal = TradeProposal.model_validate(structured)
                proposal_body = proposal.model_dump()
                bars = payload.get("bars", [])
                proposal_body["data_asof"] = bars[-1].get("timestamp") if bars else None
                proposal_body["data_source"] = "configured_market_data_provider"
                saved = self.db.add_proposal(proposal_body)
                result["proposal"] = saved
            elif isinstance(structured, dict) and structured.get("no_setup"):
                result["no_setup_reason"] = str(structured.get("reason", "No setup proposed."))[:1000]
            return result
        return await self.agent.run(task, payload)
