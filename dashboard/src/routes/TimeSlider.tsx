import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  ApiError,
  fetchLifecycleByDecisionId,
  fetchPortfolioStateAt,
  type LifecycleReconstructionPayload,
  type PortfolioEventRow,
  type PortfolioStateAtTimePayload,
} from "../lib/api";
import { formatRelativeTime, formatSignedUsd, formatPercent } from "../lib/format";
import { grafanaLogUrl } from "../lib/grafana";
import {
  TIME_WINDOWS,
  type TimeWindowKey,
  isoAtOffset,
  parseUrlT,
  windowMs,
} from "../lib/timeWindow";

// Debounce window for the scrub. 250ms strikes a balance between feeling
// responsive while the operator drags and not flooding the backend with
// reconstruction queries — every fired call hits P4.4's NFR-P1 5-minute
// ceiling so reducing call frequency matters.
const SCRUB_DEBOUNCE_MS = 250;

// Soft warning threshold for the in-flight progress UI. Backend cap is
// 5 minutes; we nudge the operator at 30s so a long query feels
// observable instead of stuck.
const SLOW_QUERY_WARN_MS = 30_000;

interface EventRowProps {
  kind: "decision" | "execution" | "pnl";
  row: PortfolioEventRow;
  selected: boolean;
  onToggle: () => void;
}

function eventLabel(kind: EventRowProps["kind"], row: PortfolioEventRow): string {
  if (kind === "decision") {
    const action = typeof row.action === "string" ? row.action : "decision";
    return action;
  }
  if (kind === "execution") {
    const t = typeof row.event_type === "string" ? row.event_type : "execution";
    return t;
  }
  const k = typeof row.pnl_kind === "string" ? row.pnl_kind : "pnl";
  return k;
}

function eventTs(row: PortfolioEventRow): string | null {
  const t = row.timestamp;
  return typeof t === "string" ? t : null;
}

function decisionId(row: PortfolioEventRow): string | null {
  return typeof row.decision_id === "string" ? row.decision_id : null;
}

function strategyOf(row: PortfolioEventRow): string | null {
  const s = row.strategy_id;
  return typeof s === "string" ? s : null;
}

function LifecycleDetail({ data }: { data: LifecycleReconstructionPayload }) {
  return (
    <div className="mt-3 rounded border border-slate-800 bg-slate-950/40 p-3">
      <div className="grid grid-cols-2 gap-3 text-[10px] uppercase tracking-wider text-slate-500 md:grid-cols-4">
        <span>
          intents: <span className="text-slate-300">{data.intents.length}</span>
        </span>
        <span>
          executions:{" "}
          <span className="text-slate-300">{data.summary.executions_count}</span>
        </span>
        <span>
          pnl events:{" "}
          <span className="text-slate-300">{data.summary.pnl_events_count}</span>
        </span>
        <span>
          filled:{" "}
          <span className="text-slate-300">
            {data.summary.has_filled ? "yes" : "no"}
          </span>
        </span>
      </div>
      {data.summary.realized_pnl_usd !== null && (
        <p className="mt-2 text-xs text-slate-300">
          realized pnl{" "}
          <span className="font-mono">
            {formatSignedUsd(data.summary.realized_pnl_usd)}
          </span>
        </p>
      )}
      {data.summary.action && (
        // Verbatim per NFR-O5 — the action is the producer's exact string.
        <p className="mt-2 text-xs text-slate-300">
          action <code className="font-mono">{data.summary.action}</code>
        </p>
      )}
    </div>
  );
}

function EventRow({ kind, row, selected, onToggle }: EventRowProps) {
  const id = decisionId(row);
  const ts = eventTs(row);
  const label = eventLabel(kind, row);
  const strategy = strategyOf(row);
  const [detail, setDetail] = useState<LifecycleReconstructionPayload | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<ApiError | null>(null);

  useEffect(() => {
    if (!selected || !id || detail || detailLoading) return;
    let alive = true;
    setDetailLoading(true);
    fetchLifecycleByDecisionId(id)
      .then((d) => alive && setDetail(d))
      .catch((e: unknown) => {
        if (!alive) return;
        setDetailError(
          e instanceof ApiError ? e : new ApiError(0, null, (e as Error).message),
        );
      })
      .finally(() => {
        if (alive) setDetailLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [selected, id, detail, detailLoading]);

  return (
    <li className="border-t border-slate-800/70 first:border-t-0">
      <div className="flex w-full items-baseline justify-between gap-3 px-1 py-2">
        <button
          type="button"
          onClick={onToggle}
          className="flex flex-1 items-baseline gap-2 text-left hover:text-slate-100"
          aria-expanded={selected}
          disabled={!id}
        >
          <span
            className={`rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${
              kind === "decision"
                ? "border-sky-700/60 bg-sky-900/50 text-sky-200"
                : kind === "execution"
                  ? "border-emerald-700/60 bg-emerald-900/40 text-emerald-200"
                  : "border-amber-700/60 bg-amber-900/40 text-amber-200"
            }`}
          >
            {kind}
          </span>
          <span className="font-mono text-xs text-slate-200">{label}</span>
          {strategy && (
            <span className="font-mono text-[10px] text-slate-500">
              {strategy}
            </span>
          )}
          {id && (
            <span className="font-mono text-[10px] text-slate-500">
              decision: {id}
            </span>
          )}
        </button>
        <div className="flex items-baseline gap-2">
          <span className="text-[10px] text-slate-500">
            {ts ? formatRelativeTime(ts) : "—"}
          </span>
          {id && (
            <a
              href={grafanaLogUrl(id)}
              target="_blank"
              rel="noopener noreferrer"
              className="rounded border border-slate-700 px-2 py-0.5 text-[10px] uppercase tracking-wider text-sky-300 hover:bg-slate-800"
              onClick={(e) => e.stopPropagation()}
              aria-label={`Open Grafana logs filtered by decision ${id}`}
            >
              logs ↗
            </a>
          )}
        </div>
      </div>
      {selected && id && (
        <div className="px-1 pb-3">
          {detailLoading && (
            <p className="text-[10px] text-slate-500">loading lifecycle…</p>
          )}
          {detailError && (
            <p className="text-[10px] text-rose-400">
              lifecycle failed — {detailError.message}
            </p>
          )}
          {detail && <LifecycleDetail data={detail} />}
        </div>
      )}
    </li>
  );
}

interface EventSliceProps {
  title: string;
  kind: EventRowProps["kind"];
  rows: PortfolioEventRow[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}

function EventSlice({ title, kind, rows, selectedId, onSelect }: EventSliceProps) {
  if (rows.length === 0) return null;
  return (
    <div className="mt-4">
      <p className="text-[10px] uppercase tracking-wider text-slate-500">
        {title}
      </p>
      <ol className="mt-1">
        {rows.map((row, i) => {
          const id = decisionId(row) ?? `${kind}-${i}`;
          const isSel = selectedId === id;
          return (
            <EventRow
              key={id + (eventTs(row) ?? "")}
              kind={kind}
              row={row}
              selected={isSel}
              onToggle={() => onSelect(isSel ? null : id)}
            />
          );
        })}
      </ol>
    </div>
  );
}

export default function TimeSlider() {
  const { t: paramT } = useParams();
  const navigate = useNavigate();

  // Anchor "now" once per mount. Re-anchoring on every render would cause
  // the slider to drift under the operator's cursor.
  const nowMsRef = useRef<number>(Date.now());
  const nowMs = nowMsRef.current;

  const [windowKey, setWindowKey] = useState<TimeWindowKey>("24h");
  const wMs = useMemo(() => windowMs(windowKey), [windowKey]);

  const initialOffset = useMemo(() => {
    const parsed = parseUrlT(paramT, nowMs);
    if (parsed === null) return 0;
    // If the URL ts is older than the chosen window, the slider pins to
    // the window edge — the user can still widen the window dropdown.
    return Math.min(parsed, wMs);
  }, [paramT, nowMs, wMs]);

  const [offsetMs, setOffsetMs] = useState<number>(initialOffset);

  // Re-clamp the offset whenever the window narrows past the current ts.
  // Without this a 7d→1h switch leaves the slider visually pinned far past
  // the new max, which is confusing.
  useEffect(() => {
    if (offsetMs > wMs) setOffsetMs(wMs);
  }, [wMs, offsetMs]);

  const atIso = useMemo(() => isoAtOffset(nowMs, offsetMs), [nowMs, offsetMs]);

  // Debounced fetch driver. Each slider input fires a 250ms timer; only the
  // last one survives, so a rapid drag triggers a single reconstruction.
  const [data, setData] = useState<PortfolioStateAtTimePayload | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [fetchStartedAt, setFetchStartedAt] = useState<number | null>(null);
  const [elapsedMs, setElapsedMs] = useState<number>(0);
  const aliveRef = useRef(true);
  useEffect(() => {
    return () => {
      aliveRef.current = false;
    };
  }, []);

  // Elapsed counter for the NFR-P1 progress UI. Only ticks while a fetch
  // is in flight.
  useEffect(() => {
    if (fetchStartedAt === null) {
      setElapsedMs(0);
      return;
    }
    setElapsedMs(Date.now() - fetchStartedAt);
    const id = window.setInterval(() => {
      setElapsedMs(Date.now() - fetchStartedAt);
    }, 1000);
    return () => window.clearInterval(id);
  }, [fetchStartedAt]);

  const debounceRef = useRef<number | null>(null);
  const runFetch = useCallback(
    (iso: string) => {
      if (debounceRef.current !== null) {
        window.clearTimeout(debounceRef.current);
      }
      debounceRef.current = window.setTimeout(() => {
        debounceRef.current = null;
        const started = Date.now();
        setFetchStartedAt(started);
        setLoading(true);
        fetchPortfolioStateAt(iso)
          .then((d) => {
            if (!aliveRef.current) return;
            setData(d);
            setError(null);
          })
          .catch((e: unknown) => {
            if (!aliveRef.current) return;
            setError(
              e instanceof ApiError
                ? e
                : new ApiError(0, null, (e as Error).message),
            );
          })
          .finally(() => {
            if (!aliveRef.current) return;
            setLoading(false);
            setFetchStartedAt(null);
          });
      }, SCRUB_DEBOUNCE_MS);
    },
    [],
  );

  // Kick off the first fetch and react to slider/window changes.
  useEffect(() => {
    runFetch(atIso);
    return () => {
      if (debounceRef.current !== null) {
        window.clearTimeout(debounceRef.current);
        debounceRef.current = null;
      }
    };
  }, [atIso, runFetch]);

  // Reflect slider state in the URL with replace:true so each scrub doesn't
  // pollute history.
  useEffect(() => {
    navigate(`/time/${encodeURIComponent(atIso)}`, { replace: true });
  }, [atIso, navigate]);

  const [selectedDecisionId, setSelectedDecisionId] = useState<string | null>(
    null,
  );

  const totalEvents = data
    ? data.recent_decisions.length +
      data.recent_executions.length +
      data.recent_pnl_events.length
    : 0;

  return (
    <section className="space-y-4">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold text-slate-100">
          operator · time slider
        </h1>
        <p className="text-xs text-slate-500">reconstruct cross-service state</p>
      </header>

      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
        <div className="flex flex-wrap items-baseline gap-3">
          <label className="text-[10px] uppercase tracking-wider text-slate-500">
            window
          </label>
          <select
            value={windowKey}
            onChange={(e) => setWindowKey(e.target.value as TimeWindowKey)}
            className="rounded border border-slate-700 bg-slate-950 px-2 py-1 font-mono text-xs text-slate-100"
          >
            {TIME_WINDOWS.map((w) => (
              <option key={w.key} value={w.key}>
                {w.label}
              </option>
            ))}
          </select>
          <span className="ml-auto font-mono text-xs text-slate-300">
            at {atIso}
          </span>
        </div>

        <input
          type="range"
          min={0}
          max={wMs}
          step={Math.max(1000, Math.floor(wMs / 1000))}
          value={offsetMs}
          // The slider's value is "ms back from now"; max position = window
          // edge = oldest. We invert the visual so left=oldest, right=now.
          onChange={(e) =>
            setOffsetMs(wMs - Number((e.target as HTMLInputElement).value))
          }
          className="mt-3 w-full accent-sky-500"
          aria-label="time slider"
        />
        <div className="flex justify-between text-[10px] text-slate-500">
          <span>{windowKey} ago</span>
          <span>now</span>
        </div>

        {loading && (
          <p
            className="mt-3 text-xs text-slate-400"
            aria-live="polite"
            role="status"
          >
            reconstructing state at {atIso} …{" "}
            <span className="font-mono">{Math.floor(elapsedMs / 1000)}s</span>
            {elapsedMs > SLOW_QUERY_WARN_MS && (
              <span className="ml-2 text-amber-400">
                taking longer than expected — NFR-P1 caps at 5 min
              </span>
            )}
          </p>
        )}
      </div>

      {error && !data && (
        <div className="rounded-lg border border-rose-700/60 bg-rose-950/40 p-4">
          <p className="text-sm text-rose-200">
            unable to load state — {error.message}
          </p>
          <button
            type="button"
            onClick={() => runFetch(atIso)}
            className="mt-2 rounded border border-rose-700/60 px-3 py-1 text-xs text-rose-100 hover:bg-rose-900/40"
          >
            retry
          </button>
        </div>
      )}

      {data && (
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
          <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
            portfolio state
          </h2>
          <div className="mt-3 grid grid-cols-2 gap-3 text-sm md:grid-cols-3">
            <div>
              <p className="text-[10px] uppercase tracking-wider text-slate-500">
                realized pnl
              </p>
              <p className="font-mono text-slate-100">
                {formatSignedUsd(data.cumulative_realized_pnl_usd)}
              </p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-slate-500">
                unrealized pnl
              </p>
              <p className="font-mono text-slate-100">
                {formatSignedUsd(data.latest_unrealized_pnl_usd)}
              </p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-slate-500">
                drawdown
              </p>
              <p className="font-mono text-slate-100">
                {formatPercent(data.current_drawdown_pct)}
              </p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-slate-500">
                current equity
              </p>
              <p className="font-mono text-slate-100">
                {formatSignedUsd(data.current_equity_usd)}
              </p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-slate-500">
                peak equity
              </p>
              <p className="font-mono text-slate-100">
                {formatSignedUsd(data.peak_equity_usd)}
              </p>
            </div>
            <div>
              <p className="text-[10px] uppercase tracking-wider text-slate-500">
                open positions
              </p>
              <p className="font-mono text-slate-100">
                {data.open_positions.length}
              </p>
            </div>
          </div>
          <p className="mt-3 text-[10px] text-slate-500">
            events evaluated:{" "}
            <span className="font-mono">{data.events_evaluated}</span>
          </p>
        </div>
      )}

      {data && (
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
          <header className="flex items-baseline justify-between">
            <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
              event chain
            </h2>
            <span className="text-[10px] uppercase tracking-wider text-slate-500">
              {totalEvents} events before {atIso}
            </span>
          </header>

          {totalEvents === 0 && (
            <p className="mt-3 text-xs text-slate-400">
              no events in this slice. try widening the window or scrubbing
              earlier.
            </p>
          )}

          <EventSlice
            title="recent decisions"
            kind="decision"
            rows={data.recent_decisions}
            selectedId={selectedDecisionId}
            onSelect={setSelectedDecisionId}
          />
          <EventSlice
            title="recent executions"
            kind="execution"
            rows={data.recent_executions}
            selectedId={selectedDecisionId}
            onSelect={setSelectedDecisionId}
          />
          <EventSlice
            title="recent pnl events"
            kind="pnl"
            rows={data.recent_pnl_events}
            selectedId={selectedDecisionId}
            onSelect={setSelectedDecisionId}
          />
        </div>
      )}
    </section>
  );
}
