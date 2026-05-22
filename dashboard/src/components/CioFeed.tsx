import { useState } from "react";
import { ApiError, CioDecision } from "../lib/api";
import { formatRelativeTime } from "../lib/format";
import PaneShell from "./PaneShell";

const ACTION_CLS: Record<string, string> = {
  execute: "text-emerald-300",
  admit: "text-emerald-300",
  veto: "text-rose-300",
  fail_safe: "text-rose-300",
  down_weight: "text-amber-300",
  skip: "text-slate-400",
};

function actionClass(action: string): string {
  return ACTION_CLS[action.toLowerCase()] ?? "text-slate-200";
}

interface DecisionRowProps {
  decision: CioDecision;
}

function DecisionRow({ decision }: DecisionRowProps) {
  const [open, setOpen] = useState(false);
  const headlineCls = actionClass(decision.action);
  return (
    <li className="border-t border-slate-800/70 first:border-t-0">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-baseline justify-between gap-3 px-1 py-2 text-left hover:bg-slate-900/60"
        aria-expanded={open}
      >
        <span className="flex items-baseline gap-2">
          <span className={`font-mono text-xs uppercase ${headlineCls}`}>
            {decision.action}
          </span>
          <span className="font-mono text-xs text-slate-300">
            {decision.strategy_id}
          </span>
          <span className="text-[10px] text-slate-500">
            conf {(decision.confidence * 100).toFixed(0)}%
          </span>
        </span>
        <span className="text-[10px] text-slate-500">
          {formatRelativeTime(decision.timestamp)}
        </span>
      </button>
      {open && (
        // Verbatim reasoning_trace — NFR-O5 forbids truncation or rephrasing.
        // whitespace-pre-wrap preserves the producer's line breaks.
        <pre className="whitespace-pre-wrap px-2 pb-3 pt-1 font-mono text-[11px] leading-snug text-slate-300">
          {decision.reasoning_trace}
        </pre>
      )}
    </li>
  );
}

interface Props {
  decisions: CioDecision[] | null;
  error: ApiError | null;
  loading: boolean;
}

export default function CioFeed({ decisions, error, loading }: Props) {
  const list = decisions ?? [];
  return (
    <PaneShell
      title="cio decisions · 24h"
      source="cio"
      loading={loading}
      error={error}
      hasData={decisions !== null}
      waitingCopy="waiting on cio…"
      footer={
        decisions
          ? `${list.length} decisions · click to expand`
          : undefined
      }
    >
      {decisions && (
        <ul className="-mx-1 max-h-72 overflow-y-auto">
          {list.map((d) => (
            <DecisionRow key={d.decision_id} decision={d} />
          ))}
          {list.length === 0 && (
            <li className="px-1 py-2 text-sm text-slate-500">
              no decisions in the last 24h
            </li>
          )}
        </ul>
      )}
    </PaneShell>
  );
}
