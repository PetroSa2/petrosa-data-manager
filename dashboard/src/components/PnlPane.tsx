import { fetchPnl } from "../lib/api";
import { useEndpoint } from "../lib/useEndpoint";
import { formatCount, formatSignedUsd } from "../lib/format";
import PaneShell from "./PaneShell";

// FR31. Portfolio scope is the default; per-strategy breakdown would require
// a separate route to enumerate strategies first — the cio /decisions/recent
// feed and the evaluator strip both surface the active strategies the
// operator can drill into via the strategy lifecycle route (#648).
export default function PnlPane() {
  const { data, error, loading } = useEndpoint(() => fetchPnl("24h"), 15_000);

  const total = data?.total_pnl_usd ?? 0;
  const totalCls =
    total > 0
      ? "text-emerald-400"
      : total < 0
        ? "text-rose-400"
        : "text-slate-300";

  return (
    <PaneShell
      title="P&L · 24h"
      source="data-manager"
      loading={loading}
      error={error}
      hasData={data !== null}
      waitingCopy="waiting on data-manager…"
      footer={
        data
          ? `${formatCount(data.fill_count)} fills · scope=${data.scope}`
          : undefined
      }
    >
      {data && (
        <div className="space-y-3">
          <div>
            <div className="text-[10px] uppercase tracking-wider text-slate-500">
              total
            </div>
            <div className={`text-3xl font-semibold ${totalCls}`}>
              {formatSignedUsd(total)}
            </div>
          </div>
          <dl className="grid grid-cols-2 gap-3 text-sm">
            <div>
              <dt className="text-[10px] uppercase tracking-wider text-slate-500">
                realized
              </dt>
              <dd className="text-slate-200">
                {formatSignedUsd(data.realized_pnl_usd)}
              </dd>
            </div>
            <div>
              <dt className="text-[10px] uppercase tracking-wider text-slate-500">
                unrealized
              </dt>
              <dd className="text-slate-200">
                {formatSignedUsd(data.unrealized_pnl_usd)}
              </dd>
            </div>
          </dl>
        </div>
      )}
    </PaneShell>
  );
}
