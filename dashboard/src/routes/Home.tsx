import { useMemo } from "react";
import { fetchRecentDecisions, fetchVerdicts } from "../lib/api";
import { useEndpoint } from "../lib/useEndpoint";
import PnlPane from "../components/PnlPane";
import DrawdownPane from "../components/DrawdownPane";
import EvaluatorStrip from "../components/EvaluatorStrip";
import CioFeed from "../components/CioFeed";

// FR23 + FR31 + FR32 + FR33 — operator dashboard home view (P5.1c).
// Composes four panes: P&L, drawdown, evaluator strip, CIO decision feed.
// The verdicts and decisions endpoints are owned here so the drawdown pane
// can fan out per-strategy requests off the same decision feed without
// duplicating polling.
export default function Home() {
  const verdicts = useEndpoint(fetchVerdicts, 10_000);
  const decisions = useEndpoint(() => fetchRecentDecisions("24h"), 15_000);

  const strategyIds = useMemo(() => {
    const seen = new Set<string>();
    for (const d of decisions.data?.decisions ?? []) {
      if (d.strategy_id) seen.add(d.strategy_id);
    }
    return Array.from(seen).sort();
  }, [decisions.data]);

  return (
    <section className="space-y-4">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold text-slate-100">
          operator · home
        </h1>
        <p className="text-xs text-slate-500">
          live · 24h window · auto-refresh
        </p>
      </header>

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <PnlPane />
        <DrawdownPane strategyIds={strategyIds} />
      </div>

      <EvaluatorStrip
        subsystems={verdicts.data?.subsystems ?? null}
        error={verdicts.error}
        loading={verdicts.loading}
      />

      <CioFeed
        decisions={decisions.data?.decisions ?? null}
        error={decisions.error}
        loading={decisions.loading}
      />
    </section>
  );
}
