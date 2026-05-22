// Time-slider window arithmetic. Pure, side-effect free. The slider is
// expressed as an offset (ms back from "now"); the URL is expressed as an
// ISO timestamp at a specific point in the past. These helpers keep the
// two representations in sync.

export type TimeWindowKey = "1h" | "6h" | "24h" | "7d";

export const TIME_WINDOWS: { key: TimeWindowKey; label: string; ms: number }[] = [
  { key: "1h", label: "1h", ms: 60 * 60 * 1000 },
  { key: "6h", label: "6h", ms: 6 * 60 * 60 * 1000 },
  { key: "24h", label: "24h", ms: 24 * 60 * 60 * 1000 },
  { key: "7d", label: "7d", ms: 7 * 24 * 60 * 60 * 1000 },
];

export function windowMs(key: TimeWindowKey): number {
  const w = TIME_WINDOWS.find((x) => x.key === key);
  // Defaulting to 24h is safe — any unknown key was already constrained
  // by the dropdown options, so this branch only fires on programming
  // errors and the fallback prevents a NaN-driven UI crash.
  return w ? w.ms : 24 * 60 * 60 * 1000;
}

// Convert (windowMs, offsetMs) → ISO timestamp anchored to a given "now".
// offsetMs is "milliseconds back from now"; 0 = now, windowMs = window edge.
// nowMs is taken as an argument so the caller can hold the anchor stable
// across renders (otherwise every render shifts the slider by Date.now()
// drift).
export function isoAtOffset(nowMs: number, offsetMs: number): string {
  return new Date(nowMs - offsetMs).toISOString();
}

// Inverse: given a URL ISO timestamp, return the offsetMs against the same
// anchor. Returns null when the param is non-parseable; caller falls back
// to "now" (offset 0). "now" is a special-cased shorthand the placeholder
// route used; preserved for backwards compatibility.
export function parseUrlT(
  t: string | undefined,
  nowMs: number,
): number | null {
  if (!t || t === "now") return 0;
  const parsed = Date.parse(t);
  if (Number.isNaN(parsed)) return null;
  const offset = nowMs - parsed;
  // Clamp slightly: negative offsets (future) shouldn't appear and would
  // confuse the slider; clamp to 0. Far-past offsets stay as-is — the
  // slider will pin to its max value visually.
  return offset < 0 ? 0 : offset;
}
