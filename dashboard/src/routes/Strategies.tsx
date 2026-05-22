import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  ApiError,
  fetchStrategyLifecycle,
  fetchStrategyList,
  type StrategyLifecyclePayload,
} from "../lib/api";
import { useEndpoint } from "../lib/useEndpoint";
import { formatRelativeTime } from "../lib/format";
import {
  deriveCurrentState,
  stateBadgeClass,
  type DerivedState,
  type StrategyState,
} from "../lib/strategyState";

type SortKey = "id" | "state" | "ts";
type SortDir = "asc" | "desc";

interface StrategyRow {
  strategy_id: string;
  state: StrategyState;
  lastTransitionAt: string | null;
  error: ApiError | null;
}

// Per-strategy lifecycle fan-out. The list is small (a handful in steady
// state; tens at most under MVP load), so a parallel fetch per row is fine.
// If the count ever grows past O(50) this should move to a dedicated
// pre-aggregated `/api/dashboard/strategies` route — capture as a follow-up.
function fetchStrategyRows(ids: string[]): Promise<StrategyRow[]> {
  return Promise.all(
    ids.map((id) =>
      fetchStrategyLifecycle(id, {
        window: null,
        types: ["lifecycle", "characterization"],
      })
        .then((payload: StrategyLifecyclePayload) => {
          const derived: DerivedState = deriveCurrentState(payload.events);
          return {
            strategy_id: id,
            state: derived.state,
            lastTransitionAt: derived.lastTransitionAt,
            error: null,
          };
        })
        .catch((e: unknown) => ({
          strategy_id: id,
          state: "unknown" as StrategyState,
          lastTransitionAt: null,
          error:
            e instanceof ApiError
              ? e
              : new ApiError(0, null, (e as Error).message),
        })),
    ),
  );
}

const HEADER_CLS =
  "px-3 py-2 text-left text-[10px] font-semibold uppercase tracking-wider text-slate-400";
const CELL_CLS = "px-3 py-2 text-sm";

function SortableHeader({
  label,
  sortKey,
  current,
  dir,
  onSort,
}: {
  label: string;
  sortKey: SortKey;
  current: SortKey;
  dir: SortDir;
  onSort: (k: SortKey) => void;
}) {
  const active = current === sortKey;
  const arrow = active ? (dir === "asc" ? "↑" : "↓") : "";
  return (
    <th className={HEADER_CLS}>
      <button
        type="button"
        onClick={() => onSort(sortKey)}
        className={`flex items-center gap-1 ${
          active ? "text-slate-200" : "text-slate-400 hover:text-slate-200"
        }`}
      >
        {label} {arrow && <span aria-hidden>{arrow}</span>}
      </button>
    </th>
  );
}

export default function Strategies() {
  const list = useEndpoint(fetchStrategyList, 30_000);
  const ids = useMemo(() => list.data?.strategy_ids ?? [], [list.data]);

  const [rows, setRows] = useState<StrategyRow[] | null>(null);
  const [rowsLoading, setRowsLoading] = useState<boolean>(false);
  const [rowsError, setRowsError] = useState<ApiError | null>(null);

  // Re-fan-out whenever the id list mutates. Stale resolutions are dropped
  // via the alive flag (mirrors useEndpoint's pattern).
  useEffect(() => {
    if (ids.length === 0) {
      setRows(list.data ? [] : null);
      return;
    }
    let alive = true;
    setRowsLoading(true);
    fetchStrategyRows(ids)
      .then((next) => {
        if (!alive) return;
        setRows(next);
        setRowsError(null);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setRowsError(
          e instanceof ApiError ? e : new ApiError(0, null, (e as Error).message),
        );
      })
      .finally(() => {
        if (alive) setRowsLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [ids, list.data]);

  const [sortKey, setSortKey] = useState<SortKey>("id");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const onSort = useCallback(
    (k: SortKey) => {
      if (k === sortKey) {
        setSortDir((d) => (d === "asc" ? "desc" : "asc"));
      } else {
        setSortKey(k);
        setSortDir("asc");
      }
    },
    [sortKey],
  );

  const sorted = useMemo(() => {
    if (!rows) return null;
    const out = [...rows];
    out.sort((a, b) => {
      let cmp = 0;
      if (sortKey === "id") {
        cmp = a.strategy_id.localeCompare(b.strategy_id);
      } else if (sortKey === "state") {
        cmp = a.state.localeCompare(b.state);
      } else {
        // ts: null sorts last regardless of direction.
        const at = a.lastTransitionAt;
        const bt = b.lastTransitionAt;
        if (at === null && bt === null) cmp = 0;
        else if (at === null) cmp = 1;
        else if (bt === null) cmp = -1;
        else cmp = at.localeCompare(bt);
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return out;
  }, [rows, sortKey, sortDir]);

  return (
    <section className="space-y-4">
      <header className="flex items-baseline justify-between">
        <h1 className="text-2xl font-semibold text-slate-100">
          operator · strategies
        </h1>
        <p className="text-xs text-slate-500">
          live · current lifecycle state per strategy
        </p>
      </header>

      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
        {list.error && !list.data && (
          <p className="text-xs text-rose-400">
            unable to load list — {list.error.message}
          </p>
        )}
        {!list.data && !list.error && (
          <div className="flex items-center gap-2 text-sm text-slate-400">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-slate-500" />
            <span>waiting on /api/v1/config/strategies…</span>
          </div>
        )}
        {list.data && ids.length === 0 && (
          <p className="text-xs text-slate-400">
            no strategies registered in the configuration store yet.
          </p>
        )}

        {rowsError && (
          <p className="mt-3 text-xs text-rose-400">
            unable to fetch per-strategy lifecycle — {rowsError.message}
          </p>
        )}

        {sorted && sorted.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr className="border-b border-slate-800">
                  <SortableHeader
                    label="strategy id"
                    sortKey="id"
                    current={sortKey}
                    dir={sortDir}
                    onSort={onSort}
                  />
                  <SortableHeader
                    label="current state"
                    sortKey="state"
                    current={sortKey}
                    dir={sortDir}
                    onSort={onSort}
                  />
                  <SortableHeader
                    label="last transition"
                    sortKey="ts"
                    current={sortKey}
                    dir={sortDir}
                    onSort={onSort}
                  />
                </tr>
              </thead>
              <tbody>
                {sorted.map((r) => (
                  <tr
                    key={r.strategy_id}
                    className="border-b border-slate-800/60 hover:bg-slate-900/60"
                  >
                    <td className={CELL_CLS}>
                      <Link
                        to={`/strategy/${encodeURIComponent(r.strategy_id)}`}
                        className="font-mono text-sky-400 hover:underline"
                      >
                        {r.strategy_id}
                      </Link>
                    </td>
                    <td className={CELL_CLS}>
                      <span
                        className={`rounded border px-2 py-0.5 font-mono text-[10px] uppercase tracking-wider ${stateBadgeClass(
                          r.state,
                        )}`}
                      >
                        {r.state}
                      </span>
                      {r.error && (
                        <span className="ml-2 text-[10px] text-rose-400">
                          fetch failed
                        </span>
                      )}
                    </td>
                    <td className={`${CELL_CLS} text-slate-300`}>
                      {r.lastTransitionAt ? (
                        <>
                          <span>{formatRelativeTime(r.lastTransitionAt)}</span>
                          <span className="ml-2 text-[10px] text-slate-500">
                            {r.lastTransitionAt}
                          </span>
                        </>
                      ) : (
                        <span className="text-slate-500">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {rowsLoading && (
              <p className="mt-3 text-[10px] text-slate-500" aria-live="polite">
                refreshing per-strategy lifecycle…
              </p>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
