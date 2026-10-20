import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import portalocker
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent.controller import AgentController
from app.agent.schemas import (
    AnalyzeRequest, ApprovalRequest, ChatRequest, ClosePositionRequest,
    RejectRequest, ResearchRequest, UpdateStopsRequest, WatchlistRequest,
)
from app.agent.tools import summarize_bars
from app.config import get_settings
from app.execution.paper_broker import PaperBroker
from app.inference.gate import InferenceGate
from app.inference.router import InferenceRouter, NoModelAvailable
from app.market_data.alpaca import AlpacaMarketData, MarketDataUnavailable
from app.market_data.feeds import ConfiguredFeedService
from app.market_data.freshness import MarketDataFreshnessError, ensure_fresh_bars
from app.market_data.monitor import MarketMonitor
from app.queue.worker import QueueWorker
from app.storage import Database
from app.backtesting.engine import evaluate_simple_moving_average

settings = get_settings()
settings.ensure_local_paths()
db = Database(settings.database_path)
router = InferenceRouter(settings, InferenceGate(settings.inference_lock_path))
agent = AgentController(router)
worker = QueueWorker(
    db, agent, settings.ai_queue_max_pending, settings.max_stale_data_seconds
)
market_provider = AlpacaMarketData(settings)
feed_service = ConfiguredFeedService(settings)
monitor = MarketMonitor(settings, db, market_provider, feed_service)
paper_broker = PaperBroker(settings, db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    instance_lock = portalocker.Lock(f"{settings.database_path}.app.lock", mode="a", timeout=0)
    try:
        with instance_lock:
            db.initialize(settings.configured_watchlist)
            worker_task = asyncio.create_task(worker.run_forever(), name="single-agent-worker")
            monitor_task = None
            if settings.market_scan_enabled:
                monitor_task = asyncio.create_task(monitor.run_forever(), name="market-monitor")
            try:
                yield
            finally:
                worker.stop()
                monitor.stop()
                worker_task.cancel()
                if monitor_task:
                    monitor_task.cancel()
                for task in [worker_task, monitor_task]:
                    if task:
                        try:
                            await task
                        except BaseException:
                            pass
    except portalocker.exceptions.LockException as exc:
        raise RuntimeError(
            "Another Trade backend process already owns this database. Stop it before starting another."
        ) from exc


app = FastAPI(
    title="Trade — Personal AI Trading Agent",
    version="0.1.0",
    description="Local-first single-agent trading research and paper trading. Live execution is not implemented.",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok", "mode": "paper",
        "paper_trading_only": settings.paper_trading_only,
        "live_trading_enabled": settings.live_trading_enabled,
        "queue_depth": db.active_queue_count() if Path(settings.database_path).exists() else 0,
    }


@app.get("/api/inference/status")
async def inference_status() -> dict[str, Any]:
    return await router.status()


@app.get("/api/market/status")
async def market_status() -> dict[str, Any]:
    return monitor.status()


@app.get("/api/news")
async def news(symbol: str | None = None, limit: int = 20) -> dict[str, Any]:
    return await feed_service.fetch_news(symbol=symbol, limit=max(1, min(limit, 100)))


@app.get("/api/events")
async def economic_events(limit: int = 20) -> dict[str, Any]:
    return await feed_service.fetch_events(limit=max(1, min(limit, 100)))


@app.post("/api/chat", status_code=202)
async def chat(request: ChatRequest) -> dict[str, Any]:
    if db.active_queue_count() >= settings.ai_queue_max_pending:
        raise HTTPException(status_code=429, detail="AI task queue is full; try again later.")
    job = db.create_job("chat", {"message": request.message})
    return {"job_id": job["id"], "status": job["status"]}


@app.get("/api/jobs")
async def list_jobs(limit: int = 50) -> dict[str, Any]:
    return {"jobs": db.list_jobs(max(1, min(limit, 200)))}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.get("/api/watchlists")
async def get_watchlist() -> dict[str, Any]:
    return {"symbols": db.list_watchlist()}


@app.post("/api/watchlists")
async def put_watchlist(request: WatchlistRequest) -> dict[str, Any]:
    try:
        symbols = db.set_watchlist(request.symbols)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.add_audit("watchlist.updated", {"symbols": symbols})
    return {"symbols": symbols}


@app.post("/api/market/scan", status_code=202)
async def run_market_scan() -> dict[str, Any]:
    try:
        return await monitor.run_once()
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        db.add_audit("market_scan.request_failed", {"error_type": type(exc).__name__})
        raise HTTPException(status_code=502, detail="Market data scan failed; inspect local diagnostics.") from exc


@app.get("/api/market/bars/{symbol}")
async def get_bars(symbol: str, limit: int = 100) -> dict[str, Any]:
    return {"symbol": symbol.upper(), "bars": db.get_bars(symbol, max(1, min(limit, 500)))}


@app.post("/api/proposals/analyze", status_code=202)
async def analyze_proposal(request: AnalyzeRequest) -> dict[str, Any]:
    symbol = request.symbol.strip().upper()
    bars = db.get_bars(symbol, limit=100)
    if len(bars) < 10:
        raise HTTPException(
            status_code=422,
            detail=f"At least 10 stored bars are required for {symbol}. Run a market scan first.",
        )
    try:
        ensure_fresh_bars(bars, request.timeframe, settings.max_stale_data_seconds)
    except MarketDataFreshnessError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    summary = summarize_bars(bars)
    if "error" in summary:
        raise HTTPException(status_code=422, detail=summary["error"])
    job = db.create_job("analyze_proposal", {
        "symbol": symbol, "timeframe": request.timeframe, "bars": bars,
        "deterministic_summary": summary,
        "news": await feed_service.fetch_news(symbol=symbol, limit=10),
        "economic_events": await feed_service.fetch_events(limit=10),
        "instruction": "Do not invent evidence or place orders.",
    })
    return {"job_id": job["id"], "status": job["status"], "symbol": symbol}


@app.get("/api/proposals")
async def list_proposals(limit: int = 100) -> dict[str, Any]:
    return {"proposals": db.list_proposals(max(1, min(limit, 200)))}


@app.post("/api/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str, request: ApprovalRequest) -> dict[str, Any]:
    try:
        return paper_broker.approve(proposal_id, request.version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, request: RejectRequest) -> dict[str, Any]:
    try:
        return paper_broker.reject(proposal_id, request.version)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/portfolio")
async def portfolio() -> dict[str, Any]:
    return db.portfolio_snapshot(settings.paper_initial_equity)


@app.get("/api/orders")
async def orders(limit: int = 100) -> dict[str, Any]:
    return {"orders": db.list_orders(max(1, min(limit, 200)))}


@app.post("/api/positions/{position_id}/stops")
async def update_stops(position_id: str, request: UpdateStopsRequest) -> dict[str, Any]:
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if row is None:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Position not found.")
        if row["status"] != "open":
            conn.rollback()
            raise HTTPException(status_code=409, detail="Position is already closed.")
        if row["side"] == "long":
            valid = request.stop_loss < row["current_price"] < request.take_profit
        else:
            valid = request.take_profit < row["current_price"] < row["stop_loss"]
        if not valid:
            conn.rollback()
            raise HTTPException(
                status_code=422,
                detail="Stops must bracket the current mark in the correct direction for the position side.",
            )
        conn.execute(
            "UPDATE positions SET stop_loss=?,take_profit=? WHERE id=?",
            (request.stop_loss, request.take_profit, position_id),
        )
        conn.commit()
    db.add_audit("paper_position.stops_updated", {
        "position_id": position_id, "stop_loss": request.stop_loss,
        "take_profit": request.take_profit, "updated_at": datetime.now(UTC).isoformat(),
    })
    return {"position_id": position_id, "status": "updated", "mode": "paper",
            "stop_loss": request.stop_loss, "take_profit": request.take_profit}


@app.post("/api/positions/{position_id}/close")
async def close_position(position_id: str, request: ClosePositionRequest) -> dict[str, Any]:
    try:
        result = db.close_position(position_id, request.simulated_exit_price)
        db.add_audit("paper_position.closed", result)
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/research/propose", status_code=202)
async def propose_research(request: ResearchRequest) -> dict[str, Any]:
    requested = [item.strip().upper() for item in request.symbols if item.strip()]
    symbols = list(dict.fromkeys(requested or db.list_watchlist()))[:30]
    summaries = []
    for symbol in symbols:
        bars = db.get_bars(symbol, limit=100)
        if bars:
            summaries.append(summarize_bars(bars))
    payload = {
        "question": request.question,
        "market": "US equities",
        "market_summaries": summaries,
        "paper_portfolio": db.portfolio_snapshot(settings.paper_initial_equity),
        "prior_experiments": db.list_experiments(),
        "news": await feed_service.fetch_news(limit=20),
        "economic_events": await feed_service.fetch_events(limit=20),
        "instruction": "Propose a research experiment only. Do not place trades or activate strategies.",
    }
    if db.active_queue_count() >= settings.ai_queue_max_pending:
        raise HTTPException(status_code=429, detail="AI task queue is full; try again later.")
    job = db.create_job("research", payload)
    return {"job_id": job["id"], "status": job["status"]}


@app.get("/api/research/experiments")
async def experiments() -> dict[str, Any]:
    return {"experiments": db.list_experiments(), "auto_promotion_enabled": False}


@app.post("/api/research/experiments", status_code=201)
async def create_experiment(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name", "")).strip()
    strategy = payload.get("strategy")
    if not name or len(name) > 150 or not isinstance(strategy, dict):
        raise HTTPException(status_code=422, detail="Provide a name and a strategy object.")
    experiment = db.create_experiment(name, strategy)
    db.add_audit("research.experiment_created", {"experiment_id": experiment["id"]})
    return experiment


@app.post("/api/research/backtest")
async def run_backtest(payload: dict[str, Any]) -> dict[str, Any]:
    symbol = str(payload.get("symbol", "")).strip().upper()
    if not symbol:
        raise HTTPException(status_code=422, detail="A symbol is required.")
    try:
        bars = db.get_bars(symbol, limit=500)
        result = evaluate_simple_moving_average(
            bars,
            fast_window=int(payload.get("fast_window", 5)),
            slow_window=int(payload.get("slow_window", 20)),
            cost_bps=float(payload.get("cost_bps", 5)),
        )
        return {"symbol": symbol, "result": result, "status": "research_only"}
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.exception_handler(NoModelAvailable)
async def no_model_handler(_, exc: NoModelAvailable):
    return JSONResponse(status_code=503, content={"detail": str(exc)})
