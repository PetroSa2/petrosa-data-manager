import { fetchLlmSpend, LlmSpendBucket } from "../lib/api";
import { useEndpoint } from "../lib/useEndpoint";
import PaneShell from "./PaneShell";

// FR63 / P5.2 (petrosa-data-manager#170): LLM spend operator visibility.
// Shows current-period spend bucketed by CIO decision type, ceiling threshold
// marker, distance-to-ceiling, and projected daily total at current burn rate.
// Data served by CIO GET /api/dashboard/llm-spend (petrosa-cio#128).

function formatUsd(v: number): string {
  if (v < 0.001) return `$${(v * 1000).toFixed(3)}m`;
  return `$${v.toFixed(4)}`;
}

function BarRow({ bucket, ceilingUsd }: { bucket: LlmSpendBucket; ceilingUsd: number }) {
  const pct = ceilingUsd > 0 ? Math.min(100, (bucket.cost_usd / ceilingUsd) * 100) : 0;
  return (
    <li className="space-y-1 py-2">
      <div className="flex items-center justify-between text-sm">
        <span className="font-mono text-slate-300">{bucket.decision_type}</span>
        <span className="text-slate-400">{formatUsd(bucket.cost_usd)}</span>
      </div>
      <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-slate-800">
        <div
          className="h-full rounded-full bg-sky-500 transition-all"
          style={{ width: `${pct}%` }}
        />
      </div>
      <div className="text-[10px] text-slate-600">
        {bucket.call_count} calls · {bucket.input_tokens.toLocaleString()} in /{" "}
        {bucket.output_tokens.toLocaleString()} out tokens
      </div>
    </li>
  );
}

export default function LlmSpendPane() {
  const { data, error, loading } = useEndpoint(fetchLlmSpend, 30_000);

  const breached = data?.ceiling_breached ?? false;

  return (
    <PaneShell
      title="LLM spend · today"
      source="cio"
      loading={loading}
      error={error}
      hasData={data !== null}
      waitingCopy="waiting on cio…"
      footer={
        data
          ? `period: ${data.period_date} · ceiling: ${formatUsd(data.ceiling_usd_per_day)}/day`
          : undefined
      }
    >
      {data && (
        <div className="space-y-3">
          <div className="flex items-baseline justify-between">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-slate-500">
                projected daily
              </div>
              <div
                className={`text-3xl font-semibold ${
                  breached ? "text-rose-400" : "text-sky-400"
                }`}
              >
                {formatUsd(data.projected_daily_usd)}
              </div>
            </div>
            <div className="text-right">
              <div className="text-[10px] uppercase tracking-wider text-slate-500">
                this period
              </div>
              <div className="text-lg font-medium text-slate-200">
                {formatUsd(data.total_cost_usd)}
              </div>
            </div>
          </div>

          {/* Ceiling threshold bar */}
          <div className="space-y-1">
            <div className="flex items-center justify-between text-[10px] text-slate-500">
              <span>0</span>
              <span
                className={breached ? "font-semibold text-rose-400" : "text-slate-400"}
              >
                ceiling {formatUsd(data.ceiling_usd_per_day)}
              </span>
            </div>
            <div className="relative h-2 w-full overflow-hidden rounded-full bg-slate-800">
              {/* Projected fill */}
              <div
                className={`h-full rounded-full transition-all ${
                  breached ? "bg-rose-500" : "bg-sky-600"
                }`}
                style={{
                  width: `${Math.min(
                    100,
                    data.ceiling_usd_per_day > 0
                      ? (data.projected_daily_usd / data.ceiling_usd_per_day) * 100
                      : 100,
                  )}%`,
                }}
              />
            </div>
            {breached && (
              <div className="rounded bg-rose-950 px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-rose-300">
                ceiling breached · CIO in deterministic-fallback mode (FR13)
              </div>
            )}
            {!breached && (
              <div className="text-[10px] text-slate-500">
                {formatUsd(data.distance_to_ceiling_usd)} until ceiling
              </div>
            )}
          </div>

          {/* Per-bucket breakdown */}
          {data.buckets.length > 0 && (
            <ul className="divide-y divide-slate-800/70">
              {data.buckets.map((b) => (
                <BarRow key={b.decision_type} bucket={b} ceilingUsd={data.ceiling_usd_per_day} />
              ))}
            </ul>
          )}
        </div>
      )}
    </PaneShell>
  );
}
