import { useParams } from "react-router-dom";

export default function Strategy() {
  const { id } = useParams();
  return (
    <section className="space-y-4">
      <h1 className="text-2xl font-semibold text-slate-100">
        Strategy lifecycle
      </h1>
      <p className="text-slate-400">
        Strategy id: <code className="text-slate-200">{id}</code>.
      </p>
      <p className="text-slate-500 text-sm">
        Implementation pending — see{" "}
        <a
          className="text-sky-400 hover:underline"
          href="https://github.com/PetroSa2/petrosa_k8s/issues/648"
        >
          #648
        </a>
        .
      </p>
    </section>
  );
}
