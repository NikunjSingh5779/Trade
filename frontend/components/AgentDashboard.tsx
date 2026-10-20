"use client";

import {
  Activity, ArrowDownRight, ArrowUpRight, Bot, Check, ChevronRight, CircleAlert,
  Clock3, Database, RefreshCw, Search, Send, ShieldCheck, Sparkles, TrendingUp,
  Wallet, X
} from "lucide-react";
import { FormEvent, useCallback, useEffect, useState } from "react";
import { api, Job, Portfolio, Proposal } from "@/lib/api";

type Health = {
  status: string; mode: string; paper_trading_only: boolean; live_trading_enabled: boolean;
  queue_depth: number;
};
type InferenceStatus = {
  local_available: boolean; local_endpoints: Array<{ base_url: string; model_id: string }>;
  hosted_fallback_enabled: boolean; selected_mode: string; inference_concurrency_limit: number;
};
type MarketStatus = { enabled: boolean; interval_seconds: number; provider_configured: boolean; regular_us_session: boolean };

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
        ? `Paper trade filled: ${proposal.symbol}, ${result.quantity} shares at simulated entry.`
        : `Proposal for ${proposal.symbol} rejected.`);
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : `Could not ${action} proposal`); }
    finally { setBusy(""); }
  }

  async function scanNow() {
    setBusy("scan"); setNotice(""); setError("");
    try {
      const result = await api<{ status: string; bar_count?: number }>("/api/market/scan", { method: "POST" });
      setNotice(result.status === "queued" ? `Market scan queued; ${result.bar_count ?? 0} bars stored.` : "Scan completed without bars.");
      await refresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Market scan failed"); }
    finally { setBusy(""); }
  }

  const pending = proposals.filter(p => p.status === "pending");
  const latestResponse = jobs.find(j => j.status === "succeeded" && j.result?.text);
  const localModel = inference?.local_endpoints?.[0]?.model_id;

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

      <div className="mb-5 grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {[
          { label: "Paper equity", value: money(portfolio?.equity ?? 100000), Icon: Wallet, note: "Simulated account" },
          { label: "Open positions", value: String(portfolio?.open_positions.length ?? 0), Icon: TrendingUp, note: `Gross ${money(portfolio?.gross_exposure ?? 0)}` },
          { label: "Unrealized P&L", value: money(portfolio?.unrealized_pnl ?? 0), Icon: (portfolio?.unrealized_pnl ?? 0) >= 0 ? ArrowUpRight : ArrowDownRight, note: "Based on current marks" },
          { label: "AI queue", value: String(health?.queue_depth ?? "—"), Icon: Activity, note: "Max one inference active" }
        ].map((item) => (
          <Panel key={item.label} className="p-4">
            <div className="mb-4 flex items-center justify-between"><span className="text-xs text-[#8e9cb2]">{item.label}</span><item.Icon size={17} className="text-[#75e0b2]"/></div>
            <div className="text-2xl font-semibold tracking-tight">{item.value}</div>
            <div className="mt-1 text-xs text-[#8e9cb2]">{item.note}</div>
          </Panel>
        ))}
      </div>

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
            {latestResponse && <div className="mt-5 rounded-xl border border-[#29384d] bg-[#0a1220] p-4">
              <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-[#75e0b2]"><Sparkles size={14}/> Latest completed response <span className="text-[#71819a]">· {latestResponse.result?.provider?.model_id ?? latestResponse.result?.provider?.provider ?? "model"}</span></div>
              <p className="whitespace-pre-wrap text-sm leading-6 text-[#cbd6e7]">{latestResponse.result?.text}</p>
            </div>}
          </Panel>

          <Panel className="p-5">
            <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center">
              <SectionTitle icon={ShieldCheck} title="Trade approval queue" note={`${pending.length} awaiting review`}/>
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
            <SectionTitle icon={Wallet} title="Open paper positions" note="Simulated only"/>
            <div className="mt-4 overflow-x-auto">
              <table className="w-full min-w-[570px] text-left text-xs">
                <thead className="text-[#71819a]"><tr className="border-b border-[#263449]"><th className="pb-3 font-medium">Symbol</th><th className="pb-3 font-medium">Side / Qty</th><th className="pb-3 font-medium">Entry</th><th className="pb-3 font-medium">Mark</th><th className="pb-3 font-medium">Stop / Target</th></tr></thead>
                <tbody>{(portfolio?.open_positions ?? []).map(pos => <tr key={pos.id} className="border-b border-[#1f2b3d] last:border-0"><td className="py-3 font-semibold">{pos.symbol}</td><td className="py-3 capitalize">{pos.side} / {pos.quantity}</td><td className="py-3">{money(pos.entry_price)}</td><td className="py-3">{money(pos.current_price)}</td><td className="py-3">{money(pos.stop_loss)} / {money(pos.take_profit)}</td></tr>)}
                  {(portfolio?.open_positions ?? []).length === 0 && <tr><td colSpan={5} className="py-6 text-center text-[#71819a]">No open paper positions yet.</td></tr>}
                </tbody>
              </table>
            </div>
          </Panel>
        </div>

        <div className="flex flex-col gap-5">
          <Panel className="p-5">
            <SectionTitle icon={Activity} title="System status"/>
            <div className="mt-4 space-y-3">
              {[
                ["Backend", health?.status === "ok" ? "Online" : "Connecting", health?.status === "ok"],
                ["Local model", localModel ?? (inference?.selected_mode === "hosted" ? "Hosted fallback" : "Not detected"), Boolean(inference?.local_available)],
                ["Market data credentials", market?.provider_configured ? "Configured" : "Not configured", Boolean(market?.provider_configured)],
                ["Market session", market?.regular_us_session ? "Regular session" : "Outside regular hours", Boolean(market?.regular_us_session)],
                ["Hosted fallback", inference?.hosted_fallback_enabled ? "Enabled by configuration" : "Disabled", !inference?.hosted_fallback_enabled],
              ].map(([name,value,ok]) => <div key={String(name)} className="flex items-center justify-between gap-3 border-b border-[#202d40] pb-3 last:border-0 last:pb-0"><div><div className="text-sm">{name}</div><div className="mt-1 text-xs text-[#71819a]">{String(value ?? "Checking…")}</div></div><span className={`h-2 w-2 rounded-full ${ok ? "bg-[#75e0b2]" : "bg-amber-400"}`}/></div>)}
            </div>
            <div className="mt-4 rounded-xl border border-[#25384b] bg-[#0a1220] p-3">
              <div className="flex items-center gap-2 text-xs font-semibold text-[#75e0b2]"><ShieldCheck size={14}/> Safety invariants</div>
              <ul className="mt-2 space-y-2 text-xs text-[#9baac0]">
                <li className="flex gap-2"><Check size={13} className="shrink-0 text-[#75e0b2]"/> One active AI inference request globally</li>
                <li className="flex gap-2"><Check size={13} className="shrink-0 text-[#75e0b2]"/> Every new position requires explicit approval</li>
                <li className="flex gap-2"><Check size={13} className="shrink-0 text-[#75e0b2]"/> Live order execution is not implemented</li>
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
