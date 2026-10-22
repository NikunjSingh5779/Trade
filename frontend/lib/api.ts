// Same-origin proxy keeps the local API token on the Next.js server, not in browser JS.
const API_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "/api/backend").replace(/\/$/, "");

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

export type AutonomousDecisionOutcome = {
  symbol: string;
  decision: string;
  status: "opened" | "no_trade" | "skipped" | string;
  side?: "long" | "short" | string;
  quantity?: number;
  simulated_price?: number;
  execution_mode?: "local_simulation" | "alpaca_paper" | string;
  broker_order_id?: string | null;
  reason?: string;
};
export type AutonomousTradingSummary = {
  status: string;
  symbols_evaluated?: number;
  explicit_no_trade?: number;
  paper_trades_opened?: number;
  decisions_skipped?: number;
  decision_outcomes?: AutonomousDecisionOutcome[];
};
export type Job = {
  id: string; task: string; status: string; created_at: string;
  result?: {
    text?: string; task?: string;
    provider?: { provider?: string; model_id?: string };
    autonomous_trading?: AutonomousTradingSummary;
  } | null;
  error?: string | null;
};
export type Proposal = {
  id: string; version: number; status: string; symbol: string; side: "long" | "short";
  entry_price: number; stop_loss: number; take_profit: number; thesis: string;
  evidence: string[]; timeframe: string; created_at: string;
};
export type PortfolioPosition = {
  id: string; symbol: string; side: string; quantity: number; entry_price: number;
  current_price: number; stop_loss: number; take_profit: number; status: string;
  execution_mode?: "local_simulation" | "alpaca_paper" | string;
  opened_at?: string; proposal_id?: string; timeframe?: string;
};
export type BrokerPosition = {
  symbol: string; qty?: string; side?: string; avg_entry_price?: string;
  current_price?: string; market_value?: string; unrealized_pl?: string;
  unrealized_plpc?: string;
};
export type ExecutionStatus = {
  mode: string; enabled: boolean; connected: boolean; configured?: boolean;
  reconciled?: boolean; error?: string; note?: string; issues?: string[];
  account?: {
    status?: string; currency?: string; equity?: string; last_equity?: string;
    cash?: string; buying_power?: string; portfolio_value?: string;
    trading_blocked?: boolean; account_blocked?: boolean; shorting_enabled?: boolean;
  };
  broker_position_count?: number;
  broker_positions?: BrokerPosition[];
  unresolved_intents?: Array<{
    proposal_id: string; symbol: string; status: string;
    created_at: string; proposal_status: string;
  }>;
  unresolved_closes?: Array<{
    position_id: string; symbol: string; status: string;
    created_at: string; side: string; quantity: number;
  }>;
};
export type Portfolio = {
  mode: string; initial_equity?: number; equity: number; cash_estimate: number;
  gross_exposure: number; unrealized_pnl: number; realized_pnl: number | null;
  open_positions: PortfolioPosition[];
  local_simulation_positions?: PortfolioPosition[];
  broker_backed_positions_pending_reconciliation?: PortfolioPosition[];
  broker_positions?: BrokerPosition[];
  execution_status?: ExecutionStatus;
  account_equity_source?: string; local_simulation_pnl_is_separate?: boolean;
};
