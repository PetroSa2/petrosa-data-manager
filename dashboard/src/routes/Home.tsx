export default function Home() {
  return (
    <section className="space-y-4">
      <h1 className="text-2xl font-semibold text-slate-100">
        Operator dashboard
      </h1>
      <p className="text-slate-400 max-w-prose">
        Placeholder home view. The real P&L + drawdown + evaluator strip + CIO
        feed surface lands in{" "}
        <a
          className="text-sky-400 hover:underline"
          href="https://github.com/PetroSa2/petrosa_k8s/issues/646"
        >
          #646
        </a>{" "}
        (P5.1c). This scaffold (#645) is intentionally thin — routing, auth
        header readout, and the build pipeline only.
      </p>
      <ul className="text-sm text-slate-500 list-disc list-inside space-y-1">
        <li>
          Time slider — <code>/time/:t</code> — sub-issue #647
        </li>
        <li>
          Strategy lifecycle view — <code>/strategy/:id</code> — sub-issue #648
        </li>
      </ul>
    </section>
  );
}
