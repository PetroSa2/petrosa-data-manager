import { useEffect, useState } from "react";

// Ingress-injected identity header. The cluster ingress is expected to
// terminate auth and stamp the operator identity into a request header.
// SPA reads it via a tiny `/auth/whoami` echo endpoint exposed by the
// data-manager API surface (added in #644). For MVP we don't manage any
// session state — the dashboard is single-operator.
const WHOAMI_PATH = "/api/auth/whoami";

export function useOperator(): string | null {
  const [operator, setOperator] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(WHOAMI_PATH, { credentials: "include" })
      .then((r) => (r.ok ? r.json() : null))
      .then((payload) => {
        if (cancelled || !payload) return;
        const op =
          typeof payload === "string"
            ? payload
            : (payload.operator ?? payload.user ?? null);
        setOperator(op);
      })
      .catch(() => {
        // Endpoint not wired yet — leave operator null so the header
        // renders the "no operator header" hint instead of crashing.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return operator;
}
