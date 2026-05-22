import { useEffect, useState } from "react";
import { ApiError, DrawdownPayload, fetchDrawdown } from "../lib/api";
import { formatPercent, formatRelativeTime } from "../lib/format";
import PaneShell from "./PaneShell";

interface Props {
  // Strategy ids surfaced upstream by the evaluator strip and the cio feed.
  // The drawdown route is per-strategy; when the upstream surfaces have no
  // strategies yet, the pane shows its empty / waiting copy.
  strategyIds: string[];
}

interface Row {
  payload: DrawdownPayload | null;
  error: ApiError | null;
}

// FR (drawdown view). Iterates the strategies surfaced upstream and renders
// each row with current drawdown vs envelope threshold. Breaches render in
// rose; non-breaches in emerald. Envelope is rendered as the [p50, p90, p99,
// p100] band when the producer attached it.
export default function DrawdownPane({ strategyIds }: Props) {
  const [rows, setRows] = useState<Record<string, Row>>({});

  useEffect(() => {
    let cancelled = false;

    async function pull() {
      const next: Record<string, Row> = {};
      await Promise.all(
        strategyIds.map(async (sid) => {
          try {
            const payload = await fetchDrawdown(sid, "24h");
            next[sid] = { payload, error: null };
          } catch (e) {
            const err =
              e instanceof ApiError
                ? e
                : new ApiError(0, null, (e as Error).message);
            next[sid] = { payload: rows[sid]?.payload ?? null, error: err };
          }
        }),
      );
      if (!cancelled) setRows(next);
    }

    if (strategyIds.length > 0) {
      void pull();
      const id = window.setInterval(() => void pull(), 30_000);
      return () => {
        cancelled = true;
        window.clearInterval(id);
      };
    }
    return () => {
      cancelled = true;
    };
    // strategyIds is a fresh array each render; serialise to avoid loops.
    // rows is intentionally not in deps — including it would re-arm every pull.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [strategyIds.join("|")]);

  const haveAnyData = Object.values(rows).some((r) => r.payload !== null);
  const anyError = Object.values(rows).find((r) => r.error)?.error ?? null;
  const loading = strategyIds.length > 0 && !haveAnyData;

  if (strategyIds.length === 0) {
    return (
      <PaneShell
        title="drawdown · 24h"
        source="data-manager"
        loading={false}
        error={null}
        hasData={false}
        waitingCopy="waiting on strategies (none active yet)…"
      >
        {null}
      </PaneShell>
    );
  }

  return (
    <PaneShell
      title="drawdown · 24h"
      source="data-manager"
      loading={loading}
      error={haveAnyData ? anyError : (anyError ?? null)}
      hasData={haveAnyData}
      waitingCopy="waiting on data-manager…"
    >
      <ul className="divide-y divide-slate-800/70">
        {strategyIds.map((sid) => {
          const row = rows[sid];
          if (!row || !row.payload) {
            return (
              <li
                key={sid}
                className="flex items-center justify-between py-2 text-sm text-slate-500"
              >
                <span className="font-mono text-slate-300">{sid}</span>
                <span className="text-xs">…</span>
              </li>
            );
          }
          const dd = row.payload.current_drawdown_pct;
          const threshold = row.payload.envelope_threshold_pct;
          const breached = row.payload.breached;
          const cls = breached
            ? "text-rose-400"
            : threshold != null
              ? "text-emerald-400"
              : "text-slate-300";
          return (
            <li
              key={sid}
              className="flex flex-col gap-1 py-2 text-sm sm:flex-row sm:items-center sm:justify-between"
            >
              <div className="flex items-center gap-2">
                <span className="font-mono text-slate-300">{sid}</span>
                {breached && (
                  <span className="rounded bg-rose-950 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-rose-300">
                    breach · {row.payload.breach_percentile ?? "n/a"}
                  </span>
                )}
              </div>
              <div className="flex items-baseline gap-3">
                <span className={`font-mono ${cls}`}>{formatPercent(dd)}</span>
                <span className="text-xs text-slate-500">
                  vs{" "}
                  {threshold != null ? formatPercent(threshold) : "no envelope"}
                </span>
                <span className="text-[10px] text-slate-600">
                  {formatRelativeTime(row.payload.timestamp)}
                </span>
              </div>
            </li>
          );
        })}
      </ul>
    </PaneShell>
  );
}
