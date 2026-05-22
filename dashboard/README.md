# petrosa-dashboard

Operator dashboard SPA for the Petrosa trading ecosystem. Colocated with
`petrosa-data-manager` because the dashboard consumes that service's API
surface (added in [#644](https://github.com/PetroSa2/petrosa_k8s/issues/644)).

This package satisfies the scaffolding portion of
[#645](https://github.com/PetroSa2/petrosa_k8s/issues/645) — routing,
single-operator auth header readout, dark-mode Tailwind, build, image —
and the home view landed in
[#646](https://github.com/PetroSa2/petrosa_k8s/issues/646) (P5.1c). The
remaining feature views land in #647 (time slider) and #648 (strategy
lifecycle).

The home view consumes four routes across two services:

- `GET /api/dashboard/portfolio/pnl?window=24h` — data-manager (#644)
- `GET /api/dashboard/portfolio/drawdown?strategy_id=...&window=24h` — data-manager (#644)
- `GET /api/dashboard/evaluator/verdicts` — petrosa-cio (#654)
- `GET /api/dashboard/decisions/recent?window=24h` — petrosa-cio (#654)

Both backends mount under `/api/dashboard/`; the cluster ingress (#655)
routes by path in production. In pure local dev the vite proxy only
points at data-manager on `:8000`, so the cio panes render their
"waiting on cio…" state unless you add a second proxy entry.

## Local development

```bash
cd dashboard
npm install            # first time only
npm run dev            # vite dev server on http://localhost:5173
                       # /api requests proxy to http://localhost:8000
                       # (a local data-manager API instance)
```

Other useful scripts:

```bash
npm run typecheck      # tsc --noEmit
npm run lint           # eslint
npm run build          # produces dashboard/dist/
npm run preview        # serves the production build on :8080
```

## Container image

```bash
docker build -t petrosa-dashboard:dev dashboard/
docker run --rm -p 8080:8080 petrosa-dashboard:dev
# open http://localhost:8080
```

The image is a multi-stage build: stage 1 produces static assets with
`vite build`, stage 2 serves them via `nginx:1.27-alpine` on port 8080.
SPA fallback (`try_files ... /index.html`) is wired in `nginx.conf`, so
client-side routes survive page refresh.

## Stack decisions (ADR)

This section is the canonical record of the architecture choices required by
#645 before implementation began. They are intentionally small and reversible
for an MVP.

### Stack: React 18 + Vite 5 + TypeScript 5

- **Rationale:** Largest ecosystem and the easiest tooling/docs surface for a
  solo-dev maintainer. The data-manager team is Python-native; React is the
  least-surprising frontend choice for non-frontend specialists.
- **Rejected alternative:** SvelteKit. Smaller bundles and a more opinionated
  framework would have helped runtime weight, but the operator dashboard is an
  internal tool where bundle size is not the binding constraint. The
  hire/contractor familiarity of React mattered more.

### Repo target: colocated under `petrosa-data-manager/dashboard/`

- **Rationale:** The SPA's only data source is the data-manager API surface
  (`/api/dashboard/*` routes added in #644). Shipping the SPA in the same repo
  removes a coordination seam during MVP — schema changes in the API and the
  consumer move together. CI for both halves runs from the same pipeline.
- **Rejected alternative:** A new `PetroSa2/petrosa-dashboard` repo. Cleaner
  boundary, but the boundary doesn't pay off until the SPA grows past
  operator-only or has multiple consumers — neither is true for MVP.

### Styling: Tailwind CSS (no UI component library)

- **Rationale:** The ticket scope explicitly resists pulling in MUI/Chakra.
  Tailwind gives us atomic styling without the framework lock-in or runtime
  weight of a component library. Operator-only UI does not need a design
  system.
- **Mode:** dark mode only (`darkMode: "class"`, `html.dark` set in
  `index.html`). Matches the Grafana operator surface aesthetic.

### Auth: ingress-injected operator identity

- **Rationale:** Single-operator MVP. The cluster ingress terminates auth and
  stamps identity into a request header; the SPA reads it via
  `/api/auth/whoami` and renders it in the chrome. No JWT, no session
  storage, no client-side auth code beyond a header echo.
- **Forward-compat:** When multi-operator becomes a requirement, this contract
  is replaced rather than extended — `useOperator()` is the only consumer.

### Routing: `react-router-dom@6` with three top-level routes

- `/` — home (#646)
- `/time/:t` — time slider (#647)
- `/strategy/:id` — strategy lifecycle (#648)

Unknown routes redirect to `/`. No sub-routing for MVP.

## Deployment

The Helm chart, ingress, and `apply-k8s-changes.yml` wiring live in the
sibling ticket
[#655](https://github.com/PetroSa2/petrosa_k8s/issues/655) under
`petrosa_k8s/k8s/dashboard/`. This package only produces the image.
