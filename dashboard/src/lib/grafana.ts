// FR35 — deep-link from a dashboard element into the Grafana/Loki
// explore view filtered by `decision_id`. The base host is configurable
// at build time via `VITE_GRAFANA_URL` (the operator picks the canonical
// host); the LogQL expression is the cross-service convention
// `{decision_id="<id>"}` that every petrosa pod emits with structured
// logging. If the env var is unset, the default points at the in-cluster
// hostname so an operator on the VPN gets a working link; out-of-cluster
// callers need to override.

const DEFAULT_GRAFANA_URL = "https://grafana.petrosa.internal";

function grafanaBase(): string {
  // Vite exposes import.meta.env as an unknown-typed bag at runtime;
  // the cast keeps the public API string-typed without forcing every
  // consumer to know vite-specific globals.
  const env = (import.meta as unknown as { env?: Record<string, string> }).env;
  const raw = env?.VITE_GRAFANA_URL;
  return (raw && raw.trim()) || DEFAULT_GRAFANA_URL;
}

// Build the explore URL. The decision_id is the only required filter;
// callers can extend with a time window once Grafana adds support — for
// MVP the explore view defaults to the last hour which lines up with
// operators' typical incident window.
export function grafanaLogUrl(decision_id: string): string {
  const base = grafanaBase().replace(/\/+$/, "");
  const expr = `{decision_id="${decision_id}"}`;
  const left = JSON.stringify({
    datasource: "loki",
    queries: [{ refId: "A", expr }],
    range: { from: "now-1h", to: "now" },
  });
  const q = new URLSearchParams({ orgId: "1", left });
  return `${base}/explore?${q.toString()}`;
}
