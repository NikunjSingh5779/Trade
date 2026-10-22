"use client";

import {
  Activity, ArrowDownRight, ArrowUpRight, Bot, Check, ChevronRight, CircleAlert,
  Clock3, Database, RefreshCw, Search, Send, ShieldCheck, Sparkles, TrendingUp,
  Wallet, X
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import PositionActions from "@/components/PositionActions";
import { api, Job, Portfolio, Proposal } from "@/lib/api";

type Health = {
  status: string; mode: string; paper_trading_only: boolean; live_trading_enabled: boolean;
  queue_depth: number;
};
type InferenceStatus = {
  local_available: boolean; local_endpoints: Array<{ base_url: string; model_id: string }>;
  hosted_fallback_enabled: boolean; selected_mode: string; inference_concurrency_limit: number;
  max_output_tokens?: number; reasoning_budget_tokens?: number; request_timeout_seconds?: number;
};
type MarketStatus = {
  enabled: boolean; interval_seconds: number; provider_configured: boolean; regular_us_session: boolean;
  autonomous_paper_trading_enabled?: boolean; autonomous_symbols?: string[];
  automated_entries_timeframe?: string; end_of_session_exit_enabled?: boolean; live_trading_enabled?: boolean;
};

const money = (n: number) => new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", maximumFractionDigits: 2
}).format(Number.isFinite(n) ? n : 0);

function Panel({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <section className={`rounded-2xl border border-[#243246] bg-[#0f1725]/90 ${className}`}>{children}</section>;
}

function SectionTitle({ icon: Icon, title, note }: { icon: typeof Activity; title: string; note?: string }) {
  return <div className="flex items-center justify-between gap-3"><div className="flex items-center gap-2"><Icon size={17} className="text-[#75e0b2]" /><h2 className="text-sm font-semibold tracking-wide">{title}</h2></div>{note && <span className="text-xs text-[#8e9cb2]">{note}</span>}</div>;
}

export default function AgentDashboard() {
  const [health, setHealth] = useState<Health | null>(null);
  const [inference, setInference] = useState<InferenceStatus | null>(null);
  const [market, setMarket] = useState<MarketStatus | null>(null);
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [proposals, setProposals] = useState<Proposal[]>([]);
  const [message, setMessage] = useState("");
  const [symbol, setSymbol] = useState("AAPL");
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    try {
      const [h, i, m, p, j, pr] = await Promise.all([
        api<Health>("/api/health"), api<InferenceStatus>("/api/inference/status"),
        api<MarketStatus>("/api/market/status"), api<Portfolio>("/api/portfolio"),
        api<{ jobs: Job[] }>("/api/jobs?limit=12"),
        api<{ proposals: Proposal[] }>("/api/proposals?limit=20")
      ]);
      setHealth(h); setInference(i); setMarket(m); setPortfolio(p);
      setJobs(j.jobs); setProposals(pr.proposals); setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not connect to backend");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 6000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function submitChat(event: FormEvent) {
    event.preventDefault();
    const prompt = message.trim();
    if (!prompt) return;
    setBusy("chat"); setNotice(""); setError("");
    try {
      const result = await api<{ job_id: string }>("/api/chat", {
        method: "POST", body: JSON.stringify({ message: prompt })
      });
      setNotice(`Request queued: ${result.job_id}`); setMessage("");
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Could not queue request"); }
    finally { setBusy(""); }
  }

  async function analyze(event: FormEvent) {
    event.preventDefault();
    const ticker = symbol.trim().toUpperCase();
    if (!ticker) return;
    setBusy("analyze"); setNotice(""); setError("");
    try {
      const result = await api<{ job_id: string; symbol: string }>("/api/proposals/analyze", {
        method: "POST", body: JSON.stringify({ symbol: ticker })
      });
      setNotice(`Analysis queued for ${result.symbol}: ${result.job_id}`);
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Could not queue analysis"); }
    finally { setBusy(""); }
  }

  async function review(proposal: Proposal, action: "approve" | "reject") {
    setBusy(proposal.id); setNotice(""); setError("");
    try {
      const result = await api<{ status?: string; mode?: string; quantity?: number; detail?: string }>(
        `/api/proposals/${proposal.id}/${action}`,
        { method: "POST", body: JSON.stringify({ version: proposal.version }) }
      );
      setNotice(action === "approve"
        ? (result.mode === "alpaca_paper"
          ? `Alpaca Paper order filled: ${proposal.symbol}, ${result.quantity} shares. Verify the broker order in your Alpaca Paper account.`
          : `Local simulation filled: ${proposal.symbol}, ${result.quantity} shares. This does not appear in Alpaca.`)
        : `Proposal for ${proposal.symbol} rejected.`);
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : `Could not ${action} proposal`); }
    finally { setBusy(""); }
  }

  async function scanNow() {
    setBusy("scan"); setNotice(""); setError("");
    try {
      const result = await api<{ status: string; bar_count?: number }>("/api/market/scan", { method: "POST" });
      setNotice(result.status === "queued"
        ? (market?.autonomous_paper_trading_enabled
          ? `Autonomous intraday scan queued; ${result.bar_count ?? 0} bars stored. Valid entries still must pass server-side risk checks.`
          : `Market scan queued; ${result.bar_count ?? 0} bars stored.`)
        : "Scan completed without fresh bars.");
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Market scan failed"); }
    finally { setBusy(""); }
  }

  const pending = proposals.filter(p => p.status === "pending");
  const latestResponse = jobs.find(
    j => j.status === "succeeded" && j.result?.text && j.task !== "market_scan"
  );
  const latestAutonomousScan = jobs.find(
    j => j.task === "market_scan" && j.status === "succeeded" && j.result?.autonomous_trading
  );
  const autonomousResult = latestAutonomousScan?.result?.autonomous_trading;
  const latestScanAttempt = jobs.find(j => j.task === "market_scan");
  const localModel = inference?.local_endpoints?.[0]?.model_id;
  const execution = portfolio?.execution_status;
  const brokerMode = portfolio?.mode === "alpaca_paper";
  const localOnlyPositions = portfolio?.local_simulation_positions ?? [];
  const brokerOfflinePositions = portfolio?.broker_backed_positions_pending_reconciliation ?? [];
  const displayedPositionCount = brokerMode
    ? (execution?.broker_position_count ?? portfolio?.open_positions.length ?? 0)
    : portfolio?.mode === "alpaca_paper_disconnected"
      ? localOnlyPositions.length
      : (portfolio?.open_positions.length ?? 0);

  return (
    <main className="mx-auto min-h-screen max-w-[1500px] px-4 py-5 md:px-8 md:py-8">
      <header className="mb-7 flex flex-col justify-between gap-5 border-b border-[#243246] pb-6 md:flex-row md:items-center">
        <div>
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[.23em] text-[#75e0b2]">
            <span className="h-2 w-2 rounded-full bg-[#75e0b2] shadow-[0_0_12px_#75e0b2]" /> Personal trading workspace
          </div>
          <h1 className="text-3xl font-semibold tracking-tight md:text-4xl">Trade<span className="text-[#75e0b2]">.</span></h1>
          <p className="mt-2 max-w-2xl text-sm text-[#8e9cb2]">One AI. One inference at a time. Evidence before action.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-2 rounded-full border border-[#2f735d] bg-[#102b26] px-3 py-2 text-xs font-medium text-[#95f0c9]"><ShieldCheck size={14}/> PAPER MODE ONLY</span>
          <button onClick={() => void refresh()} className="flex items-center gap-2 rounded-xl border border-[#33445d] px-3 py-2 text-xs hover:bg-[#162235]"><RefreshCw size={14}/> Refresh</button>
        </div>
      </header>

      {error && <div className="mb-5 flex items-start gap-2 rounded-xl border border-red-900 bg-red-950/40 p-3 text-sm text-red-200"><CircleAlert size={17} className="mt-0.5 shrink-0"/>{error}</div>}
      {notice && <div className="mb-5 rounded-xl border border-[#2f735d] bg-[#102b26] p-3 text-sm text-[#95f0c9]">{notice}</div>}

      <Panel className="mb-5 p-4">
        <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
          <div>
            <div className="flex items-center gap-2 text-sm font-semibold"><Bot size={16} className="text-[#75e0b2]"/> Autonomous intraday paper trading</div>
            <p className="mt-1 max-w-4xl text-xs leading-5 text-[#8e9cb2]">AI chooses Long, Short, or No Trade from completed five-minute candles during US regular hours. Orders go to Alpaca Paper only when that mode is explicitly enabled and reconciled; otherwise they stay in this app’s local simulation. New entries are intraday-only and live trading is disabled.</p>
            <div className="mt-3 flex flex-wrap gap-2">
              {(market?.autonomous_symbols ?? []).map(ticker => <span key={ticker} className="rounded-md border border-[#2b3c54] bg-[#0a1220] px-2 py-1 text-[10px] font-semibold tracking-wide text-[#cbd6e7]">{ticker}</span>)}
            </div>
          </div>
          <div className="flex shrink-0 flex-col gap-2 text-xs">
            <span className={`rounded-full border px-3 py-1.5 text-center font-semibold ${market?.autonomous_paper_trading_enabled ? "border-[#2f735d] bg-[#102b26] text-[#95f0c9]" : "border-[#39465a] bg-[#121d2d] text-[#a8b5c8]"}`}>{market?.autonomous_paper_trading_enabled ? "AUTONOMOUS MODE ON" : "AUTONOMOUS MODE OFF"}</span>
            <span className="text-center text-[#8e9cb2]">{market?.regular_us_session ? "US regular session open" : "Outside US regular session"}</span>
          </div>
        </div>
        {execution && <div className={`mt-4 rounded-lg border p-3 text-xs ${execution.enabled && (!execution.connected || execution.reconciled === false) ? "border-amber-700/60 bg-amber-950/30 text-amber-100" : "border-[#26384f] bg-[#0a1220] text-[#a8b5c8]"}`}>
          <div className="font-semibold">{execution.mode === "alpaca_paper" ? (execution.connected ? "Alpaca Paper API connected" : "Alpaca Paper API not connected") : "Local simulation mode"}</div>
          <p className="mt-1 leading-5">{execution.error ?? execution.note ?? "Execution status is not available."}</p>
          {!!execution.issues?.length && <div className="mt-2 space-y-1">{execution.issues.map((issue, i) => <p key={`${issue}-${i}`}>• {issue}</p>)}</div>}
          {execution.connected && execution.account?.equity && <p className="mt-2">Alpaca account equity: <strong>{money(Number(execution.account.equity))}</strong> · buying power: {money(Number(execution.account.buying_power ?? 0))}</p>}
        </div>}
      </Panel>

      <div className="mb-5 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {[
          { label: brokerMode ? "Alpaca Paper equity" : portfolio?.mode === "alpaca_paper_disconnected" ? "Local ledger equity" : "Local simulation equity", value: portfolio ? money(portfolio.equity) : "Connecting…", Icon: Wallet, note: brokerMode ? "Source: Alpaca account API" : portfolio?.mode === "alpaca_paper_disconnected" ? "Alpaca unavailable; local figures only" : "Local SQLite only; not Alpaca" },
          { label: brokerMode ? "Alpaca open positions" : portfolio?.mode === "alpaca_paper_disconnected" ? "Legacy local positions" : "Local open positions", value: portfolio ? String(displayedPositionCount) : "—", Icon: TrendingUp, note: `Gross exposure ${money(portfolio?.gross_exposure ?? 0)}` },
          { label: "Unrealized P&L", value: money(portfolio?.unrealized_pnl ?? 0), Icon: (portfolio?.unrealized_pnl ?? 0) >= 0 ? ArrowUpRight : ArrowDownRight, note: brokerMode ? "Alpaca-reported position P&L" : "Local simulation marks" },
          { label: "AI queue", value: String(health?.queue_depth ?? "—"), Icon: Activity, note: "Max one inference active" }
        ].map((item) => (
          <Panel key={item.label} className="p-4">
            <div className="mb-4 flex items-center justify-between"><span className="text-xs text-[#8e9cb2]">{item.label}</span><item.Icon size={17} className="text-[#75e0b2]"/></div>
            <div className="text-2xl font-semibold tracking-tight">{item.value}</div>
            <div className="mt-1 text-xs text-[#8e9cb2]">{item.note}</div>
          </Panel>
        ))}
      </div>

      <p className="mb-5 text-xs leading-5 text-[#8e9cb2]">Capital and risk: $100,000 is the default for local simulation and a newly created Alpaca Paper account; the actual account equity appears after a successful broker connection. At 0.25% risk per trade, $250 is the estimated stop-loss risk budget (= $100,000 × 0.0025), not the trade size. Position size is also limited by stop distance and exposure caps.</p>

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1.2fr)_minmax(360px,.8fr)]">
        <div className="flex flex-col gap-5">
          <Panel className="p-5">
            <SectionTitle icon={Bot} title="Talk to your agent" note="Single-agent queue"/>
            <p className="mt-2 text-sm leading-6 text-[#8e9cb2]">Ask for market context, strategy research, or an explanation. Requests are queued and handled sequentially by the selected model.</p>
            <form onSubmit={submitChat} className="mt-4">
              <textarea value={message} onChange={e => setMessage(e.target.value)} rows={3} maxLength={8000}
                placeholder="Ask the agent to explain a setup, compare strategy ideas, or review the latest market snapshot…"
                className="w-full resize-y rounded-xl border border-[#2b3c54] bg-[#09111e] px-4 py-3 text-sm outline-none placeholder:text-[#60728d] focus:border-[#75e0b2]"/>
              <div className="mt-3 flex items-center justify-between gap-3">
                <span className="text-xs text-[#71819a]">AI output is research, not an order.</span>
                <button disabled={busy === "chat" || !message.trim()} className="flex items-center gap-2 rounded-xl bg-[#75e0b2] px-4 py-2.5 text-sm font-semibold text-[#082019] hover:bg-[#94f0c7] disabled:opacity-50"><Send size={15}/> {busy === "chat" ? "Queuing…" : "Send request"}</button>
              </div>
            </form>
            {market?.autonomous_paper_trading_enabled && <div className="mt-5 rounded-xl border border-[#29384d] bg-[#0a1220] p-4">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-xs font-semibold text-[#75e0b2]"><Activity size={14}/> Latest autonomous scan outcome</div>
                <span className="text-[10px] text-[#71819a]">{latestAutonomousScan ? latestAutonomousScan.id : latestScanAttempt ? `Latest scan: ${latestScanAttempt.status}` : "No scan yet"}</span>
              </div>
              {autonomousResult ? <>
                <div className="grid grid-cols-3 gap-2">
                  <div className="rounded-lg border border-[#243246] bg-[#0f1725] p-3">
                    <div className="text-[10px] uppercase text-[#8e9cb2]">Paper entries</div>
                    <div className="mt-1 text-xl font-semibold text-[#95f0c9]">{autonomousResult.paper_trades_opened ?? 0}</div>
                  </div>
                  <div className="rounded-lg border border-[#243246] bg-[#0f1725] p-3">
                    <div className="text-[10px] uppercase text-[#8e9cb2]">No trade</div>
                    <div className="mt-1 text-xl font-semibold">{autonomousResult.explicit_no_trade ?? 0}</div>
                  </div>
                  <div className="rounded-lg border border-[#243246] bg-[#0f1725] p-3">
                    <div className="text-[10px] uppercase text-[#8e9cb2]">Skipped</div>
                    <div className="mt-1 text-xl font-semibold">{autonomousResult.decisions_skipped ?? 0}</div>
                  </div>
                </div>
                {autonomousResult.status !== "processed" && <p className="mt-3 text-xs text-amber-200">Scan outcome: {autonomousResult.status}. No automated entry was confirmed.</p>}
                <div className="mt-3 space-y-2">
                  {(autonomousResult.decision_outcomes ?? []).map((outcome, index) => <div key={`${outcome.symbol}-${index}`} className="flex flex-col gap-1 rounded-lg border border-[#243246] bg-[#0f1725] p-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs font-semibold text-[#e4ebf6]">{outcome.symbol}</span>
                      {outcome.side && <span className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${outcome.side === "long" ? "bg-[#143a30] text-[#8ee9c4]" : "bg-[#3a242b] text-[#ffb0ba]"}`}>{outcome.side}</span>}
                      <span className={`rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${outcome.status === "opened" ? "bg-[#143a30] text-[#8ee9c4]" : outcome.status === "skipped" ? "bg-amber-950/50 text-amber-200" : "bg-[#1a2637] text-[#a8b5c8]"}`}>{outcome.status === "opened" ? "Paper position opened" : outcome.status === "no_trade" ? "No trade" : outcome.status}</span>
                      {outcome.quantity && outcome.simulated_price && <span className="text-[10px] text-[#8e9cb2]">{outcome.quantity} shares · {outcome.execution_mode === "alpaca_paper" ? "Alpaca Paper fill" : "local simulated fill"} {money(outcome.simulated_price)}</span>}
                    </div>
                    {outcome.reason && <p className="text-xs leading-5 text-[#8e9cb2]">{outcome.reason}</p>}
                  </div>)}
                </div>
                <p className="mt-3 text-[10px] leading-4 text-[#71819a]">Only entries listed as “Paper position opened” were executed. Local simulations stay in SQLite; Alpaca Paper fills appear in the Alpaca simulated account. No live orders are sent.</p>
              </> : <p className="text-xs leading-5 text-[#8e9cb2]">{latestScanAttempt?.status === "failed" ? `The latest market scan failed: ${latestScanAttempt.error ?? "check the backend terminal for details"}` : "Waiting for a completed autonomous scan with an execution summary. The raw AI answer alone does not prove a paper trade was opened."}</p>}
            </div>}
            {latestResponse && <div className="mt-5 rounded-xl border border-[#29384d] bg-[#0a1220] p-4">
              <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-[#75e0b2]"><Sparkles size={14}/> Latest completed response <span className="text-[#71819a]">· {latestResponse.result?.provider?.model_id ?? latestResponse.result?.provider?.provider ?? "model"}</span></div>
              <p className="whitespace-pre-wrap text-sm leading-6 text-[#cbd6e7]">{latestResponse.result?.text}</p>
            </div>}
          </Panel>

          <Panel className="p-5">
            <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
              <SectionTitle icon={ShieldCheck} title="Proposal review queue" note={`${pending.length} awaiting review · manual analyses`}/>
              <form onSubmit={analyze} className="flex items-center gap-2">
                <div className="relative"><Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-[#71819a]"/><input value={symbol} onChange={e => setSymbol(e.target.value.toUpperCase())} maxLength={12} aria-label="Symbol to analyze" className="w-24 rounded-lg border border-[#2b3c54] bg-[#09111e] py-2 pl-8 pr-2 text-xs uppercase outline-none focus:border-[#75e0b2]"/></div>
                <button disabled={busy === "analyze"} className="rounded-lg border border-[#39516c] px-3 py-2 text-xs hover:bg-[#162235] disabled:opacity-50">{busy === "analyze" ? "Queuing…" : "Analyze symbol"}</button>
              </form>
            </div>
            <div className="mt-4 space-y-3">
              {pending.length === 0 && <div className="rounded-xl border border-dashed border-[#2b3c54] p-7 text-center"><ShieldCheck size={22} className="mx-auto mb-2 text-[#62748e]"/><p className="text-sm text-[#9baac0]">No pending proposals</p><p className="mt-1 text-xs text-[#71819a]">A model-generated idea appears here only after valid JSON and schema checks.</p></div>}
              {pending.map(p => <div key={p.id} className="rounded-xl border border-[#2b3c54] bg-[#0a1220] p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div><div className="flex items-center gap-2"><span className="text-lg font-semibold">{p.symbol}</span><span className={`rounded-md px-2 py-1 text-[10px] font-bold uppercase ${p.side === "long" ? "bg-[#143a30] text-[#8ee9c4]" : "bg-[#3a242b] text-[#ffb0ba]"}`}>{p.side}</span><span className="text-xs text-[#71819a]">{p.timeframe}</span></div><p className="mt-2 text-sm leading-5 text-[#a8b5c8]">{p.thesis}</p></div>
                  <span className="rounded-md border border-amber-700/50 bg-amber-900/20 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-amber-200">Review required</span>
                </div>
                <div className="mt-4 grid grid-cols-3 gap-2">
                  {[["Entry",p.entry_price],["Stop",p.stop_loss],["Target",p.take_profit]].map(([label,value]) => <div key={String(label)} className="rounded-lg bg-[#121d2d] p-2"><div className="text-[10px] uppercase text-[#71819a]">{label}</div><div className="mt-1 text-sm font-medium">{money(Number(value))}</div></div>)}
                </div>
                {!!p.evidence?.length && <div className="mt-3 text-xs leading-5 text-[#8e9cb2]"><span className="font-semibold text-[#aab8cc]">Evidence: </span>{p.evidence.join(" · ")}</div>}
                <div className="mt-4 flex gap-2">
                  <button disabled={!!busy} onClick={() => void review(p, "approve")} className="flex flex-1 items-center justify-center gap-2 rounded-lg bg-[#75e0b2] px-3 py-2.5 text-xs font-bold text-[#082019] hover:bg-[#94f0c7] disabled:opacity-50"><Check size={14}/> {busy === p.id ? "Processing…" : "Approve paper trade"}</button>
                  <button disabled={!!busy} onClick={() => void review(p, "reject")} className="flex items-center justify-center gap-2 rounded-lg border border-[#39465a] px-3 py-2.5 text-xs hover:bg-[#1a2637] disabled:opacity-50"><X size={14}/> Reject</button>
                </div>
              </div>)}
            </div>
          </Panel>

          <Panel className="p-5">
            <SectionTitle icon={Wallet} title={brokerMode ? "Open Alpaca Paper positions" : "Open local simulation positions"} note={brokerMode ? "Broker-reported account" : "SQLite only · not sent to Alpaca"}/>
            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[800px] text-left text-xs">
                <thead className="text-[#71819a]"><tr className="border-b border-[#263449]"><th className="pb-3 font-medium">Symbol</th><th className="pb-3 font-medium">Side / Qty</th><th className="pb-3 font-medium">Entry</th><th className="pb-3 font-medium">Mark</th><th className="pb-3 font-medium">Stop / Target</th><th className="pb-3 font-medium">Execution</th><th className="pb-3 font-medium">Manage</th></tr></thead>
                <tbody>{(portfolio?.open_positions ?? []).map(pos => <tr key={pos.id} className="border-b border-[#1f2b3d] last:border-0"><td className="py-3 font-semibold">{pos.symbol}</td><td className="py-3 capitalize">{pos.side} / {pos.quantity}</td><td className="py-3">{money(pos.entry_price)}</td><td className="py-3">{money(pos.current_price)}</td><td className="py-3">{money(pos.stop_loss)} / {money(pos.take_profit)}</td><td className="py-3">{pos.execution_mode === "alpaca_paper" ? "Alpaca Paper" : "Local simulation"}</td><td className="py-3"><PositionActions position={pos} onChanged={() => void refresh()}/></td></tr>)}
                  {(portfolio?.open_positions ?? []).length === 0 && <tr><td colSpan={7} className="py-6 text-center text-[#71819a]">{brokerMode ? "No Alpaca Paper positions are currently tracked." : "No local simulation positions are open."}</td></tr>}
                </tbody>
              </table>
            </div>
          </Panel>
          {brokerOfflinePositions.length > 0 && <Panel className="p-5">
            <SectionTitle icon={CircleAlert} title="Alpaca positions pending reconciliation" note="Broker connection required"/>
            <p className="mt-2 text-xs leading-5 text-amber-100">These positions were created through Alpaca Paper, but the broker API is currently unavailable or their current state could not be confirmed. Do not close them at a local mark. Reconnect Alpaca and confirm broker status first.</p>
            <div className="mt-4 space-y-2">{brokerOfflinePositions.map(pos => <div key={pos.id} className="rounded-lg border border-[#2b3c54] bg-[#0a1220] p-3 text-xs"><strong>{pos.symbol} · {pos.side.toUpperCase()} · {pos.quantity} shares</strong><p className="mt-1 text-[#a8b5c8]">Recorded entry {money(pos.entry_price)} · last stored mark {money(pos.current_price)} · stop {money(pos.stop_loss)} · target {money(pos.take_profit)}</p></div>)}</div>
          </Panel>}
          {brokerMode && (portfolio?.broker_positions?.length ?? 0) > 0 && <Panel className="p-5">
            <SectionTitle icon={Activity} title="Actual Alpaca Paper account positions" note="Broker API"/>
            <p className="mt-2 text-xs leading-5 text-[#8e9cb2]">These rows come directly from Alpaca&apos;s Paper account. Any “broker-only” or mismatched position must be reconciled before this app can open another Alpaca Paper trade. They are never silently copied into the local ledger.</p>
            <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[500px] text-left text-xs">
              <thead className="text-[#71819a]"><tr className="border-b border-[#263449]"><th className="pb-2">Symbol</th><th className="pb-2">Side / Qty</th><th className="pb-2">Avg entry</th><th className="pb-2">Current</th><th className="pb-2">Unrealized P&amp;L</th></tr></thead>
              <tbody>{(portfolio?.broker_positions ?? []).map((bp, index) => <tr key={`${bp.symbol}-${index}`} className="border-b border-[#1f2b3d] last:border-0"><td className="py-2 font-semibold">{bp.symbol}</td><td className="py-2 capitalize">{bp.side ?? "—"} / {bp.qty ?? "—"}</td><td className="py-2">{bp.avg_entry_price ? money(Number(bp.avg_entry_price)) : "—"}</td><td className="py-2">{bp.current_price ? money(Number(bp.current_price)) : "—"}</td><td className="py-2">{bp.unrealized_pl ? money(Number(bp.unrealized_pl)) : "$0.00"}</td></tr>)}</tbody>
            </table></div>
          </Panel>}
          {localOnlyPositions.length > 0 && <Panel className="p-5">
            <SectionTitle icon={CircleAlert} title="Legacy local-only positions" note="Not in Alpaca"/>
            <p className="mt-2 text-xs leading-5 text-amber-100">These rows are local simulations from before Alpaca Paper routing was enabled. They are not broker holdings and are excluded from Alpaca equity/P&L. Review each before using “Close at mark”; that closes only the local simulation.</p>
            <div className="mt-4 space-y-3">{localOnlyPositions.map(pos => <div key={pos.id} className="rounded-xl border border-[#2b3c54] bg-[#0a1220] p-3">
              <div className="mb-3 flex flex-wrap items-center justify-between gap-2"><div className="text-sm font-semibold">{pos.symbol} · {pos.side.toUpperCase()} · {pos.quantity} shares</div><span className="rounded border border-amber-700/60 px-2 py-1 text-[10px] text-amber-100">LOCAL ONLY</span></div>
              <div className="grid grid-cols-2 gap-2 text-xs text-[#a8b5c8] sm:grid-cols-4"><div>Entry <strong>{money(pos.entry_price)}</strong></div><div>Mark <strong>{money(pos.current_price)}</strong></div><div>Stop <strong>{money(pos.stop_loss)}</strong></div><div>Target <strong>{money(pos.take_profit)}</strong></div></div>
              <div className="mt-3"><PositionActions position={pos} onChanged={() => void refresh()}/></div>
            </div>)}</div>
          </Panel>}
        </div>

        <div className="flex flex-col gap-5">
          <Panel className="p-5">
            <SectionTitle icon={Activity} title="System status"/>
            <div className="mt-4 space-y-3">
              {[
                ["Backend", health?.status === "ok" ? "Online" : "Connecting", health?.status === "ok"],
                ["Local model", localModel ?? (inference?.selected_mode === "hosted" ? "Hosted fallback" : "Not detected"), Boolean(inference?.local_available)],
                ["Market data credentials", market?.provider_configured ? "Configured" : "Not configured", Boolean(market?.provider_configured)],
                ["Order execution", execution?.mode === "alpaca_paper" ? (execution.connected ? "Alpaca Paper account" : "Alpaca Paper configured but offline") : "Local simulation only", execution?.mode !== "alpaca_paper" || Boolean(execution.connected)],
                ["Broker reconciliation", execution?.mode !== "alpaca_paper" ? "Not applicable in local mode" : execution.reconciled ? "Matched" : "Blocked: review issues above", execution?.mode !== "alpaca_paper" || Boolean(execution.reconciled)],
                ["Market session", market?.regular_us_session ? "Regular session" : "Outside regular hours", Boolean(market?.regular_us_session)],
                ["AI generation budget", inference?.max_output_tokens != null
                  ? `${inference.max_output_tokens.toLocaleString()} max output · ${inference.reasoning_budget_tokens?.toLocaleString() ?? "—"} reasoning · ${inference.request_timeout_seconds ?? "—"}s timeout`
                  : "Waiting for backend settings", inference?.max_output_tokens != null],
                ["Hosted fallback", inference?.hosted_fallback_enabled ? "Enabled by configuration" : "Disabled", !inference?.hosted_fallback_enabled],
              ].map(([name,value,ok]) => <div key={String(name)} className="flex items-center justify-between gap-3 border-b border-[#202d40] pb-3 last:border-0 last:pb-0"><div><div className="text-sm">{name}</div><div className="mt-1 text-xs text-[#71819a]">{String(value ?? "Checking…")}</div></div><span className={`h-2 w-2 rounded-full ${ok ? "bg-[#75e0b2]" : "bg-amber-400"}`}/></div>)}
            </div>
            <div className="mt-4 rounded-xl border border-[#25384b] bg-[#0a1220] p-3">
              <div className="flex items-center gap-2 text-xs font-semibold text-[#75e0b2]"><ShieldCheck size={14}/> Safety invariants</div>
              <ul className="mt-2 space-y-2 text-xs text-[#9baac0]">
                <li className="flex gap-2"><Check size={13} className="shrink-0 text-[#75e0b2]"/> One active AI inference request globally</li>
                <li className="flex gap-2"><Check size={13} className="shrink-0 text-[#75e0b2]"/>{market?.autonomous_paper_trading_enabled
                  ? "Automated entries still require backend risk checks; manual proposals require approval"
                  : "New positions require explicit manual approval"}</li>
                <li className="flex gap-2"><Check size={13} className="shrink-0 text-[#75e0b2]"/> Live orders are never supported; broker mode can target Alpaca Paper only</li>
                <li className="flex gap-2"><Check size={13} className="shrink-0 text-[#75e0b2]"/> Strategy promotion is manual only</li>
              </ul>
            </div>
            <button onClick={() => void scanNow()} disabled={busy === "scan"} className="mt-4 flex w-full items-center justify-center gap-2 rounded-xl border border-[#3a516d] px-4 py-3 text-sm hover:bg-[#162235] disabled:opacity-50"><RefreshCw size={15}/>{busy === "scan" ? "Scanning…" : "Run market scan now"}</button>
            {!market?.provider_configured && <p className="mt-2 text-xs leading-5 text-amber-200/80">Add valid US market-data credentials to .env before scanning.</p>}
          </Panel>

          <Panel className="p-5">
            <SectionTitle icon={Clock3} title="Agent task queue" note="Newest first"/>
            <div className="mt-4 space-y-3">
              {jobs.slice(0, 8).map(job => <div key={job.id} className="flex items-start gap-3 border-b border-[#202d40] pb-3 last:border-0 last:pb-0">
                <div className="mt-0.5 rounded-lg bg-[#162235] p-2 text-[#8fa4c0]"><ChevronRight size={14}/></div>
                <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="text-xs font-medium">{job.task.replaceAll("_", " ")}</span><span className={`rounded px-1.5 py-0.5 text-[10px] uppercase ${job.status === "succeeded" ? "bg-[#143a30] text-[#8ee9c4]" : job.status === "failed" ? "bg-[#3a242b] text-[#ffb0ba]" : "bg-[#283247] text-[#afc3e1]"}`}>{job.status}</span></div>
                  <p className="mt-1 break-all text-[10px] text-[#71819a]">{job.id}</p>
                  {job.error && <p className="mt-1 text-xs text-red-300">{job.error}</p>}
                </div><Database size={14} className="mt-1 shrink-0 text-[#546a87]"/>
              </div>)}
              {jobs.length === 0 && <p className="py-5 text-center text-xs text-[#71819a]">No AI jobs yet. Send a request to start.</p>}
            </div>
          </Panel>

          <Panel className="p-5">
            <SectionTitle icon={Sparkles} title="Research loop" note="No auto-promotion"/>
            <p className="mt-3 text-sm leading-6 text-[#8e9cb2]">Strategy ideas must be tested against historical data, costs and out-of-sample periods. Experimental results are stored separately and never replace the active strategy automatically.</p>
            <div className="mt-4 flex items-center gap-2 rounded-xl border border-[#2b3c54] bg-[#0a1220] p-3 text-xs text-[#a6b7ce]"><ShieldCheck size={16} className="text-[#75e0b2]"/> Strategy promotion remains disabled in Phase 1.</div>
          </Panel>
        </div>
      </div>

      <footer className="mt-8 flex flex-col justify-between gap-2 border-t border-[#243246] pt-4 text-xs text-[#60728d] sm:flex-row">
        <span>TRADE / PHASE 1 · LOCAL-FIRST · US EQUITIES</span>
        <span className="flex items-center gap-2"><Activity size={12}/> {inference?.inference_concurrency_limit ?? 1} global inference slot</span>
      </footer>
    </main>
  );
}
