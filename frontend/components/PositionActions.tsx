"use client";

import { FormEvent, useState } from "react";
import { LogOut, SlidersHorizontal } from "lucide-react";
import { api } from "@/lib/api";

type Position = {
  id: string; symbol: string; current_price: number;
  stop_loss: number; take_profit: number; execution_mode?: string;
};

export default function PositionActions({
  position, onChanged
}: { position: Position; onChanged: () => void }) {
  const [stop, setStop] = useState(String(position.stop_loss));
  const [target, setTarget] = useState(String(position.take_profit));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function updateStops(event: FormEvent) {
    event.preventDefault();
    setBusy(true); setError("");
    try {
      await api(`/api/positions/${position.id}/stops`, {
        method: "POST",
        body: JSON.stringify({ stop_loss: Number(stop), take_profit: Number(target) })
      });
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not update simulated stops");
    } finally { setBusy(false); }
  }

  async function closeAtMark() {
    setBusy(true); setError("");
    try {
      await api(`/api/positions/${position.id}/close`, {
        method: "POST",
        body: JSON.stringify({ simulated_exit_price: position.current_price })
      });
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not close simulated position");
    } finally { setBusy(false); }
  }

  return (
    <div className="min-w-[210px] space-y-2">
      {position.execution_mode === "alpaca_paper" ? (
        <p className="text-[10px] leading-4 text-[#8e9cb2]">Stop/target are broker-managed bracket orders. Edit them in Alpaca Paper.</p>
      ) : (
        <form onSubmit={updateStops} className="grid grid-cols-[1fr_1fr_auto] gap-1">
          <label className="sr-only" htmlFor={`stop-${position.id}`}>Stop loss</label>
          <input id={`stop-${position.id}`} type="number" step="0.01" min="0" value={stop}
            onChange={e => setStop(e.target.value)} aria-label="Stop loss"
            className="w-full rounded border border-[#31435a] bg-[#09111e] px-2 py-1.5 text-xs"/>
          <label className="sr-only" htmlFor={`target-${position.id}`}>Take profit</label>
          <input id={`target-${position.id}`} type="number" step="0.01" min="0" value={target}
            onChange={e => setTarget(e.target.value)} aria-label="Take profit"
            className="w-full rounded border border-[#31435a] bg-[#09111e] px-2 py-1.5 text-xs"/>
          <button title="Save simulated stop and target" disabled={busy} className="rounded border border-[#39516d] px-2 text-[#a6f0ce] disabled:opacity-50"><SlidersHorizontal size={14}/></button>
        </form>
      )}
      <button onClick={() => void closeAtMark()} disabled={busy}
        className="flex w-full items-center justify-center gap-1.5 rounded border border-[#4a3c47] px-2 py-1.5 text-xs text-[#ffbdc6] hover:bg-[#2b1923] disabled:opacity-50">
        <LogOut size={13}/> {position.execution_mode === "alpaca_paper" ? "Close via Alpaca Paper" : "Close at local mark"}
      </button>
      {error && <p className="max-w-[230px] whitespace-normal text-[11px] text-red-300">{error}</p>}
    </div>
  );
}
