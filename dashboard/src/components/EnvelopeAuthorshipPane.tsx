// P4.6-AC4.b/c/d — envelope-authorship pane (#204).
//
// Consumes GET /api/dashboard/envelope-authorship and renders three
// sections: current envelopes per strategy_or_portfolio_key, pending
// change requests (with accept/reject CTAs that POST to the existing
// /api/envelopes/{change_id}/{accept,reject,accept-with-modification}
// endpoints), and the decided history with pagination.
//
// Operator identification + signed_action_id are pulled from the
// `useOperator` hook (already wired into the rest of the dashboard).
// The flow is verbatim — no client-side validation logic beyond
// "rejection_reason required when rejecting"; the backend rejects
// malformed inputs with RFC-7807 problem JSON, which the existing
// ApiError surface renders to the operator.

import { useState } from "react";
import {
  EnvelopeAuthorshipPayload,
  PendingEnvelopeChangeView,
  fetchEnvelopeAuthorship,
  postEnvelopeAccept,
  postEnvelopeReject,
} from "../lib/api";
import { useEndpoint } from "../lib/useEndpoint";
import { useOperator } from "../lib/operator";
import PaneShell from "./PaneShell";

export default function EnvelopeAuthorshipPane({
  filterKey,
  limit = 50,
}: {
  filterKey?: string;
  limit?: number;
}) {
  const { data, error, loading, generation, refresh } = useEndpoint(
    () => fetchEnvelopeAuthorship(filterKey, limit),
    30_000,
  );

  return (
    <PaneShell
      title="Envelope Authorship"
      source="data-manager · /api/dashboard/envelope-authorship"
      loading={loading}
      error={error}
      hasData={generation > 0}
      waitingCopy="waiting on data-manager…"
    >
      {data && (
        <div className="space-y-6">
          <CurrentSection data={data} />
          <PendingSection data={data} onResolved={refresh} />
          <HistorySection data={data} />
        </div>
      )}
    </PaneShell>
  );
}

function CurrentSection({ data }: { data: EnvelopeAuthorshipPayload }) {
  return (
    <section>
      <h3 className="text-xs uppercase tracking-wider text-slate-400">
        Current ({data.current.length})
      </h3>
      {data.current.length === 0 ? (
        <p className="mt-1 text-sm text-slate-500">
          no active envelopes
        </p>
      ) : (
        <ul className="mt-1 space-y-1">
          {data.current.map((env) => (
            <li
              key={env.envelope_id}
              className="rounded border border-slate-800 bg-slate-900/50 px-3 py-2 text-sm"
            >
              <div className="flex items-baseline justify-between">
                <span className="font-semibold text-slate-200">
                  {env.strategy_or_portfolio_key}
                </span>
                <span className="text-xs text-slate-500">
                  v{env.version} · {env.source}
                </span>
              </div>
              <pre className="mt-1 text-xs text-slate-400 whitespace-pre-wrap break-words">
                {JSON.stringify(env.value, null, 2)}
              </pre>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function PendingSection({
  data,
  onResolved,
}: {
  data: EnvelopeAuthorshipPayload;
  onResolved: () => void;
}) {
  return (
    <section>
      <h3 className="text-xs uppercase tracking-wider text-amber-300">
        Pending ({data.pending.length})
      </h3>
      {data.pending.length === 0 ? (
        <p className="mt-1 text-sm text-slate-500">
          no proposals awaiting action
        </p>
      ) : (
        <ul className="mt-1 space-y-2">
          {data.pending.map((change) => (
            <PendingRow
              key={change.change_id}
              change={change}
              onResolved={onResolved}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function PendingRow({
  change,
  onResolved,
}: {
  change: PendingEnvelopeChangeView;
  onResolved: () => void;
}) {
  const operator = useOperator();
  const [busy, setBusy] = useState<"accept" | "reject" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // signed_action_id is a per-action identifier captured by the backend
  // (AC1.e). We produce a stable per-button-click one — the backend is the
  // source of truth for the audit trail, not the client.
  const signedActionId = (kind: string) =>
    `dashboard:${kind}:${change.change_id}:${Date.now()}`;

  const handleAccept = async () => {
    if (!operator) {
      setActionError("operator not identified — set your operator id first");
      return;
    }
    setBusy("accept");
    setActionError(null);
    try {
      await postEnvelopeAccept(change.change_id, {
        operator_id: operator,
        signed_action_id: signedActionId("accept"),
      });
      onResolved();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const handleReject = async () => {
    if (!operator) {
      setActionError("operator not identified — set your operator id first");
      return;
    }
    const reason = window.prompt("Rejection reason (required):");
    if (!reason) return;
    setBusy("reject");
    setActionError(null);
    try {
      await postEnvelopeReject(change.change_id, {
        operator_id: operator,
        signed_action_id: signedActionId("reject"),
        rejection_reason: reason,
      });
      onResolved();
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <li className="rounded border border-amber-900/50 bg-slate-900/60 px-3 py-2 text-sm">
      <div className="flex items-baseline justify-between">
        <span className="font-semibold text-slate-200">
          {change.strategy_or_portfolio_key}
        </span>
        <span className="text-xs text-slate-500">
          {new Date(change.created_at).toISOString()}
        </span>
      </div>
      <pre className="mt-1 text-xs text-slate-400 whitespace-pre-wrap break-words">
        {JSON.stringify(change.proposed_envelope_value, null, 2)}
      </pre>
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          onClick={handleAccept}
          disabled={busy !== null}
          className="rounded bg-emerald-700 px-3 py-1 text-xs font-semibold text-slate-50 hover:bg-emerald-600 disabled:opacity-50"
        >
          {busy === "accept" ? "accepting…" : "accept"}
        </button>
        <button
          type="button"
          onClick={handleReject}
          disabled={busy !== null}
          className="rounded bg-rose-700 px-3 py-1 text-xs font-semibold text-slate-50 hover:bg-rose-600 disabled:opacity-50"
        >
          {busy === "reject" ? "rejecting…" : "reject"}
        </button>
      </div>
      {actionError && (
        <p className="mt-2 text-xs text-rose-400">
          action failed — {actionError}
        </p>
      )}
    </li>
  );
}

function HistorySection({ data }: { data: EnvelopeAuthorshipPayload }) {
  return (
    <section>
      <h3 className="text-xs uppercase tracking-wider text-slate-400">
        History ({data.history.length})
      </h3>
      {data.history.length === 0 ? (
        <p className="mt-1 text-sm text-slate-500">no decided changes yet</p>
      ) : (
        <ul className="mt-1 space-y-1">
          {data.history.map((change) => {
            const resolved = change.resolution;
            const verb =
              change.status === "accepted" ? "accepted" : "rejected";
            const color =
              change.status === "accepted"
                ? "text-emerald-300"
                : "text-rose-300";
            return (
              <li
                key={change.change_id}
                className="rounded border border-slate-800 bg-slate-900/40 px-3 py-2 text-xs"
              >
                <div className="flex items-baseline justify-between">
                  <span className="font-semibold text-slate-200">
                    {change.strategy_or_portfolio_key}
                  </span>
                  <span className={color}>{verb}</span>
                </div>
                <div className="mt-0.5 text-slate-500">
                  {resolved
                    ? `${resolved.operator_id} · ${new Date(resolved.decided_at).toISOString()}`
                    : "(missing resolution metadata)"}
                </div>
                {resolved?.rejection_reason && (
                  <div className="mt-1 text-slate-400 italic">
                    reason: {resolved.rejection_reason}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {data.next_cursor && (
        <p className="mt-2 text-[10px] text-slate-500">
          more available — cursor: {data.next_cursor}
        </p>
      )}
    </section>
  );
}
