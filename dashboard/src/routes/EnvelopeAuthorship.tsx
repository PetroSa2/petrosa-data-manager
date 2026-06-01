// P4.6-AC4.b/c/d — envelope-authorship page (#204).
// Renders the EnvelopeAuthorshipPane standalone. Accepts ?key= via URL
// search params so an operator can deep-link to a single strategy's
// envelope-authorship view.

import { useSearchParams } from "react-router-dom";
import EnvelopeAuthorshipPane from "../components/EnvelopeAuthorshipPane";

export default function EnvelopeAuthorship() {
  const [searchParams] = useSearchParams();
  const filterKey = searchParams.get("key") ?? undefined;

  return (
    <main className="mx-auto max-w-6xl px-4 py-6">
      <EnvelopeAuthorshipPane filterKey={filterKey} />
    </main>
  );
}
