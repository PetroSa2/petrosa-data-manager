import { ApiError, EvaluatorVerdict } from "../lib/api";
import { formatRelativeTime } from "../lib/format";
import PaneShell from "./PaneShell";

// FR23 + NFR-O5. The evaluator strip renders the per-subsystem verdict and
// the producer's verbatim `evidence` text. No truncation, no rephrasing —
// operator-facing text comes from the producing service unchanged.

const VERDICT_CLS: Record<string, string> = {
  healthy: "border-emerald-700 bg-emerald-950/40 text-emerald-200",
  degraded: "border-amber-700 bg-amber-950/40 text-amber-200",
  unhealthy: "border-rose-700 bg-rose-950/40 text-rose-200",
  unknown: "border-slate-700 bg-slate-900/40 text-slate-400",
};

function verdictClass(verdict: string): string {
  return VERDICT_CLS[verdict.toLowerCase()] ?? VERDICT_CLS.unknown;
}

interface Props {
  subsystems: EvaluatorVerdict[] | null;
  error: ApiError | null;
  loading: boolean;
}

export default function EvaluatorStrip({ subsystems, error, loading }: Props) {
  const list = subsystems ?? [];
  return (
    <PaneShell
      title="evaluator strip"
      source="cio"
      loading={loading}
      error={error}
      hasData={subsystems !== null}
      waitingCopy="waiting on cio…"
      footer={
        subsystems
          ? `${list.length}/8 subsystems reporting`
          : undefined
      }
    >
      {subsystems && (
        <ul className="grid grid-cols-2 gap-2 md:grid-cols-4">
          {list.map((v) => (
            <li
              key={v.subsystem}
              className={`rounded border px-2 py-2 ${verdictClass(v.verdict)}`}
              title={`${v.subsystem} — ${v.verdict} @ ${v.last_tick_at}`}
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-mono text-[11px] text-slate-200">
                  {v.subsystem}
                </span>
                <span className="text-[10px] uppercase tracking-wider">
                  {v.verdict}
                </span>
              </div>
              {/* Verbatim evidence — NFR-O5 forbids rephrasing. */}
              <p className="mt-1 text-[11px] leading-snug text-slate-300">
                {v.evidence}
              </p>
              <p className="mt-1 text-[10px] text-slate-500">
                {formatRelativeTime(v.last_tick_at)}
              </p>
            </li>
          ))}
          {list.length === 0 && (
            <li className="col-span-full text-sm text-slate-500">
              no verdicts recorded yet
            </li>
          )}
        </ul>
      )}
    </PaneShell>
  );
}
