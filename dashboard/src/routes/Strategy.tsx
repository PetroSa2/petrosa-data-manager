import { useCallback, useMemo } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  ApiError,
  fetchStrategyLifecycle,
  type StrategyLifecyclePayload,
  type TimelineEventType,
} from "../lib/api";
import { useEndpoint } from "../lib/useEndpoint";
import { formatRelativeTime } from "../lib/format";
import {
  deriveCurrentState,
  stateBadgeClass,
  type StrategyTransition,
} from "../lib/strategyState";

// Only the state-change event types are rendered here. Auxiliary types
// (config, intent, decision, execution) come back from the same endpoint
// but would dominate the visual timeline; the index page links to them via
// the time slider instead.
const STATE_EVENT_TYPES: TimelineEventType[] = ["lifecycle", "characterization"];

function TransitionRow({ transition }: { transition: StrategyTransition }) {
  const navigate = useNavigate();
  const open = useCallback(() => {
    navigate(`/time/${encodeURIComponent(transition.ts)}`);
  }, [navigate, transition.ts]);

  const badge = stateBadgeClass(transition.state);
  return (
    <li className="border-t border-slate-800/70 first:border-t-0">
      <button
        type="button"
        onClick={open}
        className="flex w-full flex-col gap-1 px-1 py-3 text-left hover:bg-slate-900/60"
        aria-label={`Open time slider at ${transition.ts}`}
      >
        <div className="flex items-baseline justify-between gap-3">
          <span className="flex items-baseline gap-2">
            <span
              className={`rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${badge}`}
            >
              {transition.state}
            </span>
            <span className="font-mono text-[10px] text-slate-500">
              {transition.event_type}
            </span>
            {transition.decision_id && (
              <span className="font-mono text-[10px] text-slate-500">
                decision: {transition.decision_id}
              </span>
            )}
          </span>
          <span className="text-[10px] text-slate-500">
            {formatRelativeTime(transition.ts)} · {transition.ts}
          </span>
        </div>
        {transition.reason && (
          // Verbatim — NFR-O5 forbids truncation or rephrasing.
          <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words text-xs text-slate-300">
            {transition.reason}
          </pre>
        )}
      </button>
    </li>
  );
}

export default function Strategy() {
  const { id } = useParams();
  const strategyId = id ?? "";
  const fetcher = useCallback(
    () =>
      fetchStrategyLifecycle(strategyId, {
        window: null,
        types: STATE_EVENT_TYPES,
      }),
    [strategyId],
  );
  const endpoint = useEndpoint<StrategyLifecyclePayload>(fetcher, 15_000);

  const derived = useMemo(() => {
    if (!endpoint.data) return null;
    return deriveCurrentState(endpoint.data.events);
  }, [endpoint.data]);

  const notFound =
    endpoint.error instanceof ApiError && endpoint.error.status === 404;
  if (notFound) {
    return (
      <section className="space-y-4">
        <header className="flex items-baseline justify-between">
          <h1 className="text-2xl font-semibold text-slate-100">
            strategy lifecycle
          </h1>
          <Link
            to="/strategies"
            className="text-xs text-sky-400 hover:underline"
          >
            ← all strategies
          </Link>
        </header>
        <div className="rounded-lg border border-rose-700/60 bg-rose-950/40 p-4">
          <p className="text-sm text-rose-200">
            strategy <code className="text-rose-100">{strategyId}</code> not
            found.
          </p>
          <p className="mt-1 text-xs text-rose-300/80">
            the configuration store has no record of this id. it may have
            never been registered, or it may have been renamed.
          </p>
        </div>
      </section>
    );
  }

  const badgeCls = derived ? stateBadgeClass(derived.state) : "";

  return (
    <section className="space-y-4">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold text-slate-100">
          strategy lifecycle
        </h1>
        <Link to="/strategies" className="text-xs text-sky-400 hover:underline">
          ← all strategies
        </Link>
      </header>

      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
        <p className="text-xs uppercase tracking-wider text-slate-500">
          strategy id
        </p>
        <p className="mt-1 font-mono text-sm text-slate-100">{strategyId}</p>

        <div className="mt-4 flex items-baseline justify-between gap-3">
          <div>
            <p className="text-xs uppercase tracking-wider text-slate-500">
              current state
            </p>
            <p className="mt-1">
              {derived ? (
                <span
                  className={`rounded border px-2 py-1 font-mono text-xs uppercase tracking-wider ${badgeCls}`}
                >
                  {derived.state}
                </span>
              ) : (
                <span className="text-xs text-slate-500">loading…</span>
              )}
            </p>
          </div>
          {derived?.lastTransitionAt && (
            <div className="text-right">
              <p className="text-xs uppercase tracking-wider text-slate-500">
                last transition
              </p>
              <p className="mt-1 text-xs text-slate-300">
                {formatRelativeTime(derived.lastTransitionAt)}
              </p>
              <p className="text-[10px] text-slate-500">
                {derived.lastTransitionAt}
              </p>
            </div>
          )}
        </div>
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
        <header className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
            transitions
          </h2>
          <span className="text-[10px] uppercase tracking-wider text-slate-500">
            /api/dashboard/strategy/{strategyId}/lifecycle
          </span>
        </header>

        {endpoint.error && !endpoint.data && (
          <p className="mt-3 text-xs text-rose-400">
            unable to load — {endpoint.error.message}
          </p>
        )}
        {endpoint.error && endpoint.data && (
          <p className="mt-3 text-xs text-amber-400">
            last refresh failed ({endpoint.error.status || "network"}); showing
            previous value
          </p>
        )}

        {!endpoint.data && !endpoint.error && (
          <div className="mt-3 flex items-center gap-2 text-sm text-slate-400">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-slate-500" />
            <span>waiting on transitions…</span>
          </div>
        )}

        {derived && derived.transitions.length === 0 && (
          <p className="mt-3 text-xs text-slate-400">
            no transitions yet. the strategy may be registered but not
            characterized, or the lifecycle writer has not started recording
            (P1.2 dependency).
          </p>
        )}

        {derived && derived.transitions.length > 0 && (
          <ol className="mt-2">
            {derived.transitions.map((t) => (
              <TransitionRow key={t.event_id} transition={t} />
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}
