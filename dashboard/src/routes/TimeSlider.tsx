import { useParams } from "react-router-dom";

export default function TimeSlider() {
  const { t } = useParams();
  return (
    <section className="space-y-4">
      <h1 className="text-2xl font-semibold text-slate-100">Time slider</h1>
      <p className="text-slate-400">
        Reconstruct past state at <code className="text-slate-200">{t}</code>.
      </p>
      <p className="text-slate-500 text-sm">
        Implementation pending — see{" "}
        <a
          className="text-sky-400 hover:underline"
          href="https://github.com/PetroSa2/petrosa_k8s/issues/647"
        >
          #647
        </a>
        .
      </p>
    </section>
  );
}
