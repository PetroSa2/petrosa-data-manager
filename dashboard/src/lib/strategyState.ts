import type { TimelineEvent } from "./api";

// Strategy lifecycle states. The vocabulary is the producer's verbatim enum
// (petrosa-cio/cio/core/lifecycle.py::LifecycleState). The ticket's draft
// list ("trial") is reconciled to the actual producer value ("on_trial").
export type StrategyState =
  | "registered"
  | "characterized"
  | "admitted"
  | "on_trial"
  | "graduated"
  | "demoted"
  | "retired"
  | "rejected"
  | "unknown";

// One state transition derived from a single lifecycle / characterization
// event. Auxiliary types (config, intent, decision, execution) are not state
// transitions and are excluded by the API call's `types` filter.
export interface StrategyTransition {
  ts: string;
  state: StrategyState;
  // Verbatim source-of-truth text from the producer. Empty string when the
  // event has no reasoning attached — never substitute filler copy.
  reason: string;
  decision_id: string | null;
  event_id: string;
  event_type: TimelineEvent["type"];
}

const KNOWN_STATES = new Set<StrategyState>([
  "registered",
  "characterized",
  "admitted",
  "on_trial",
  "graduated",
  "demoted",
  "retired",
  "rejected",
]);

function asKnownState(raw: unknown): StrategyState {
  if (typeof raw !== "string") return "unknown";
  return KNOWN_STATES.has(raw as StrategyState)
    ? (raw as StrategyState)
    : "unknown";
}

function asString(raw: unknown): string {
  return typeof raw === "string" ? raw : "";
}

function reasoningToText(raw: unknown): string {
  // The producer's `reasoning` is a free-form dict; the operator-facing view
  // serializes it verbatim per NFR-O5. A plain string passes through; a dict
  // is JSON-stringified so nothing is silently dropped.
  if (raw === undefined || raw === null) return "";
  if (typeof raw === "string") return raw;
  try {
    return JSON.stringify(raw);
  } catch {
    return "";
  }
}

// Map a single timeline event to a transition, or null when the event does
// not represent a state change. `lifecycle` events carry `to_state`;
// `characterization` events implicitly mark the strategy as characterized.
function eventToTransition(e: TimelineEvent): StrategyTransition | null {
  if (e.type === "lifecycle") {
    const state = asKnownState(e.payload.to_state);
    return {
      ts: e.ts,
      state,
      reason: reasoningToText(e.payload.reasoning),
      decision_id: asString(e.payload.decision_id) || null,
      event_id: e.event_id,
      event_type: "lifecycle",
    };
  }
  if (e.type === "characterization") {
    return {
      ts: e.ts,
      state: "characterized",
      reason: reasoningToText(e.payload.summary ?? e.payload.notes),
      decision_id: null,
      event_id: e.event_id,
      event_type: "characterization",
    };
  }
  return null;
}

// Walks events in chronological order and produces:
// - `state`: the most-recent known state (defaults to `registered` when at
//   least one config audit row exists, else `unknown`).
// - `lastTransitionAt`: ISO timestamp of the most-recent transition, or null.
// - `transitions`: filtered list of lifecycle + characterization transitions,
//   ascending by ts.
export interface DerivedState {
  state: StrategyState;
  lastTransitionAt: string | null;
  transitions: StrategyTransition[];
}

export function deriveCurrentState(events: TimelineEvent[]): DerivedState {
  const transitions: StrategyTransition[] = [];
  let hasConfig = false;
  for (const e of events) {
    if (e.type === "config") hasConfig = true;
    const t = eventToTransition(e);
    if (t) transitions.push(t);
  }
  // The API sorts ascending by (ts, event_id); preserve that order here.
  transitions.sort((a, b) =>
    a.ts === b.ts ? a.event_id.localeCompare(b.event_id) : a.ts.localeCompare(b.ts),
  );

  const last = transitions.length > 0 ? transitions[transitions.length - 1] : null;
  if (last) {
    return {
      state: last.state,
      lastTransitionAt: last.ts,
      transitions,
    };
  }
  return {
    state: hasConfig ? "registered" : "unknown",
    lastTransitionAt: null,
    transitions,
  };
}

// Tailwind colour class for a state badge. Centralised so the detail view
// and the index page render identically.
export function stateBadgeClass(state: StrategyState): string {
  switch (state) {
    case "graduated":
      return "bg-emerald-900/60 text-emerald-200 border-emerald-700/60";
    case "admitted":
    case "on_trial":
      return "bg-sky-900/60 text-sky-200 border-sky-700/60";
    case "characterized":
      return "bg-indigo-900/60 text-indigo-200 border-indigo-700/60";
    case "registered":
      return "bg-slate-800 text-slate-200 border-slate-700";
    case "demoted":
      return "bg-amber-900/60 text-amber-200 border-amber-700/60";
    case "retired":
    case "rejected":
      return "bg-rose-900/60 text-rose-200 border-rose-700/60";
    default:
      return "bg-slate-800 text-slate-400 border-slate-700";
  }
}
