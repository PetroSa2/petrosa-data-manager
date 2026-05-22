import { ReactNode } from "react";
import { ApiError } from "../lib/api";

export interface PaneShellProps {
  title: string;
  source: string;
  loading: boolean;
  error: ApiError | null;
  hasData: boolean;
  waitingCopy: string;
  children: ReactNode;
  footer?: ReactNode;
}

// Shared chrome for every pane. Renders the title, a "waiting on …" skeleton
// when no data has loaded yet, an error banner on failure, and stale-data
// hinting when an error follows a successful payload (so the pane keeps
// showing the last known value rather than blanking — see AC: "no silent
// blank panes").
export default function PaneShell({
  title,
  source,
  loading,
  error,
  hasData,
  waitingCopy,
  children,
  footer,
}: PaneShellProps) {
  const showSkeleton = !hasData;
  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
      <header className="flex items-baseline justify-between">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-slate-300">
          {title}
        </h2>
        <span className="text-[10px] uppercase tracking-wider text-slate-500">
          {source}
        </span>
      </header>

      {showSkeleton ? (
        <div className="mt-3 flex items-center gap-2 text-sm text-slate-400">
          <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-slate-500" />
          <span>{waitingCopy}</span>
        </div>
      ) : (
        <div className="mt-3">{children}</div>
      )}

      {error && hasData && (
        <p className="mt-3 text-xs text-amber-400">
          last refresh failed ({error.status || "network"}); showing previous
          value
        </p>
      )}
      {error && !hasData && (
        <p className="mt-3 text-xs text-rose-400">
          unable to load — {error.message}
        </p>
      )}

      {footer && <div className="mt-3 text-xs text-slate-500">{footer}</div>}

      {loading && hasData && (
        <span className="sr-only" aria-live="polite">
          refreshing
        </span>
      )}
    </section>
  );
}
