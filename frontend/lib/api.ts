const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000").replace(/\/$/, "");

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    cache: "no-store"
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof data.detail === "string" ? data.detail : `Request failed (${response.status})`;
    throw new Error(detail);
  }
  return data as T;
}

export type Job = {
  id: string; task: string; status: string; created_at: string;
  result?: { text?: string; task?: string; provider?: { provider?: string; model_id?: string } } | null;
  error?: string | null;
};
export type Proposal = {
  id: string; version: number; status: string; symbol: string; side: "long" | "short";
  entry_price: number; stop_loss: number; take_profit: number; thesis: string;
  evidence: string[]; timeframe: string; created_at: string;
};
export type Portfolio = {
  mode: string; equity: number; cash_estimate: number; gross_exposure: number;
  unrealized_pnl: number; realized_pnl: number; open_positions: Array<{
    id: string; symbol: string; side: string; quantity: number; entry_price: number;
    current_price: number; stop_loss: number; take_profit: number; status: string;
  }>;
};
