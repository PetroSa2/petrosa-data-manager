// Operator dashboard API client. Both petrosa-data-manager and petrosa-cio
// expose `/api/dashboard/...` routes (the former added in #644, the latter
// in #654). The cluster ingress routes by path in production; the vite dev
// proxy only points at data-manager today, so the cio panes will show their
// "waiting on cio…" state in pure local dev. That gap is wiring, not contract.

export type Window = "1h" | "6h" | "24h" | "7d" | "30d";

export interface PnlPayload {
  scope: "portfolio" | "strategy";
  strategy_id: string | null;
  window: string | null;
  from: string | null;
  to: string | null;
  realized_pnl_usd: number;
  unrealized_pnl_usd: number;
  total_pnl_usd: number;
  fill_count: number;
}

export interface DrawdownPayload {
  strategy_id: string;
  current_drawdown_pct: number;
  envelope_threshold_pct: number | null;
  breach_percentile: string | null;
  breached: boolean;
  peak_equity_usd: number;
  current_equity_usd: number;
  events_evaluated: number;
  timestamp: string;
  reason: string;
  envelope: number[] | null;
  window?: string | null;
}

export interface EvaluatorVerdict {
  subsystem: string;
  verdict: string;
  last_tick_at: string;
  // The producer's verbatim evidence text — NFR-O5 forbids rephrasing.
  evidence: string;
}

export interface VerdictsPayload {
  subsystems: EvaluatorVerdict[];
}

export interface CioDecision {
  decision_id: string;
  strategy_id: string;
  action: string;
  reasoning_trace: string;
  confidence: number;
  timestamp: string;
}

export interface DecisionsPayload {
  window: string;
  strategy_id: string | null;
  decisions: CioDecision[];
}

export interface ProblemJson {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public problem: ProblemJson | null,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function getJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { Accept: "application/json" } });
  } catch (e) {
    throw new ApiError(0, null, `network error: ${(e as Error).message}`);
  }
  if (!res.ok) {
    let problem: ProblemJson | null = null;
    try {
      const body = (await res.json()) as { detail?: ProblemJson } | ProblemJson;
      problem =
        (body as { detail?: ProblemJson }).detail ?? (body as ProblemJson);
    } catch {
      // RFC 7807 problem JSON not available; fall through with status only.
    }
    throw new ApiError(
      res.status,
      problem,
      problem?.detail ?? problem?.title ?? `HTTP ${res.status}`,
    );
  }
  return (await res.json()) as T;
}

export function fetchPnl(window: Window = "24h"): Promise<PnlPayload> {
  return getJson<PnlPayload>(`/api/dashboard/portfolio/pnl?window=${window}`);
}

export function fetchStrategyPnl(
  strategy_id: string,
  window: Window = "24h",
): Promise<PnlPayload> {
  const q = new URLSearchParams({ window, strategy_id });
  return getJson<PnlPayload>(`/api/dashboard/portfolio/pnl?${q.toString()}`);
}

export function fetchDrawdown(
  strategy_id: string,
  window: Window = "24h",
): Promise<DrawdownPayload> {
  const q = new URLSearchParams({ window, strategy_id });
  return getJson<DrawdownPayload>(
    `/api/dashboard/portfolio/drawdown?${q.toString()}`,
  );
}

export function fetchVerdicts(): Promise<VerdictsPayload> {
  return getJson<VerdictsPayload>(`/api/dashboard/evaluator/verdicts`);
}

export function fetchRecentDecisions(
  window: Window = "24h",
): Promise<DecisionsPayload> {
  return getJson<DecisionsPayload>(
    `/api/dashboard/decisions/recent?window=${window}`,
  );
}

// Per-strategy lifecycle timeline (FR36, P5.1e). The data-manager route
// returns chronologically-merged events from six audit-trail collections;
// payload shape is intentionally loose because each event type carries its
// own per-source fields.
export type TimelineEventType =
  | "config"
  | "characterization"
  | "lifecycle"
  | "intent"
  | "decision"
  | "execution";

export interface TimelineEvent {
  ts: string;
  type: TimelineEventType;
  source: string;
  event_id: string;
  payload: Record<string, unknown>;
}

export interface StrategyLifecyclePayload {
  strategy_id: string;
  events: TimelineEvent[];
  window: string | null;
  next_cursor: string | null;
}

export function fetchStrategyLifecycle(
  strategy_id: string,
  options: { window?: string | null; types?: TimelineEventType[] } = {},
): Promise<StrategyLifecyclePayload> {
  const q = new URLSearchParams();
  // Omit window to get full history; pass an explicit value when the caller
  // wants a rolling window.
  if (options.window !== undefined && options.window !== null) {
    q.set("window", options.window);
  }
  if (options.types && options.types.length > 0) {
    q.set("types", options.types.join(","));
  }
  q.set("limit", "200");
  const qs = q.toString();
  return getJson<StrategyLifecyclePayload>(
    `/api/dashboard/strategy/${encodeURIComponent(strategy_id)}/lifecycle${qs ? `?${qs}` : ""}`,
  );
}

// Strategy index. The data-manager `/api/v1/config/strategies` route returns
// every strategy known to the configuration store. P5.1a never shipped a
// dedicated dashboard-side list endpoint; the SPA derives current_state per
// strategy by fanning out lifecycle queries on top of this list (#648 P5.1e).
export interface StrategyListPayload {
  strategy_ids: string[];
}

export function fetchStrategyList(): Promise<StrategyListPayload> {
  return getJson<StrategyListPayload>(`/api/v1/config/strategies`);
}

// Time-slider reconstruction (FR34, P5.1d). The shape mirrors
// PortfolioStateAtTime.to_dict() in data_manager/portfolio/state_service.py;
// the recent_* arrays are deliberately loose because each row carries
// per-collection fields. Sub-second drift between Mongo timestamps and the
// `at` query parameter is expected — the slider quantises to seconds.
export interface PortfolioEventRow {
  // Every recent_* row has either `timestamp` (decisions/executions) or
  // `event_ts`/equivalent (pnl_events); the slider only depends on
  // `decision_id` for the deep-link and on `timestamp`-ish for sort order.
  // Keep the type open so the renderer can pull whatever the collection
  // happens to expose without forcing a backend change.
  [key: string]: unknown;
  decision_id?: string;
  timestamp?: string;
}

export interface PortfolioStateAtTimePayload {
  at: string;
  strategy_id: string | null;
  cumulative_realized_pnl_usd: number;
  latest_unrealized_pnl_usd: number;
  current_equity_usd: number;
  peak_equity_usd: number;
  current_drawdown_pct: number;
  open_positions: PortfolioEventRow[];
  recent_decisions: PortfolioEventRow[];
  recent_executions: PortfolioEventRow[];
  recent_pnl_events: PortfolioEventRow[];
  events_evaluated: number;
}

export function fetchPortfolioStateAt(
  at: string,
  strategy_id?: string | null,
): Promise<PortfolioStateAtTimePayload> {
  const q = new URLSearchParams({ at });
  if (strategy_id) q.set("strategy_id", strategy_id);
  return getJson<PortfolioStateAtTimePayload>(
    `/api/dashboard/portfolio/state?${q.toString()}`,
  );
}

// FR63: LLM spend pane (P5.2, petrosa-data-manager#170). Route lives in CIO
// at GET /api/dashboard/llm-spend; cluster ingress routes by path.
export interface LlmSpendBucket {
  decision_type: string;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  call_count: number;
}

export interface LlmSpendPayload {
  period_date: string;
  ceiling_usd_per_day: number;
  total_cost_usd: number;
  projected_daily_usd: number;
  ceiling_breached: boolean;
  distance_to_ceiling_usd: number;
  buckets: LlmSpendBucket[];
}

export function fetchLlmSpend(): Promise<LlmSpendPayload> {
  return getJson<LlmSpendPayload>(`/api/dashboard/llm-spend`);
}

// Per-decision lifecycle reconstruction (FR34 detail panel; P4.3 #603).
export interface LifecycleReconstructionPayload {
  decision_id: string;
  decision: Record<string, unknown>;
  intents: Record<string, unknown>[];
  executions: Record<string, unknown>[];
  pnl_events: Record<string, unknown>[];
  summary: {
    action: string | null;
    strategy_id: string | null;
    decision_timestamp: string | null;
    executions_count: number;
    pnl_events_count: number;
    has_filled: boolean;
    realized_pnl_usd: number | null;
  };
}

export function fetchLifecycleByDecisionId(
  decision_id: string,
): Promise<LifecycleReconstructionPayload> {
  return getJson<LifecycleReconstructionPayload>(
    `/api/dashboard/lifecycle/${encodeURIComponent(decision_id)}`,
  );
}
