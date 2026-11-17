import { useState } from "react";

import { useSession } from "../auth";
import { useAction } from "../hooks";
import type { Evidence, EvidenceRef } from "../types";
import { Badge, ErrorNote } from "./ui";

export const INJECTION_NOTE =
  "This passage contains text that looks like instructions. Kaizeval treats it as data only and never follows it.";

export function FlagBadges({ flags }: { flags: string[] | null | undefined }) {
  if (!flags || flags.length === 0) return null;
  return (
    <>
      {flags.map((flag) => (
        <Badge key={flag} tone={flag === "possible_injection" ? "warn" : "neutral"}
          title={flag === "possible_injection" ? INJECTION_NOTE : undefined}>
          {flag.replace(/_/g, " ")}
        </Badge>
      ))}
    </>
  );
}

/** A "chunk:<id>" reference that loads and shows the passage when opened. */
export function EvidenceLink({ reference }: { reference: string }) {
  const { api } = useSession();
  const [open, setOpen] = useState(false);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const action = useAction();
  const chunkId = reference.startsWith("chunk:") ? reference.slice(6) : reference;

  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (next && !evidence) {
      void action.run(async () => setEvidence(await api.evidence(chunkId)));
    }
  };

  return (
    <span className="evidence-link">
      <button type="button" className="link-button" aria-expanded={open} onClick={toggle}>
        passage {chunkId.slice(0, 8)}
      </button>
      {open && (
        <span className="evidence-body">
          {action.busy && <span className="muted">Loading passage…</span>}
          <ErrorNote error={action.error} />
          {evidence && (
            <>
              <span className="muted small">
                {evidence.filename ?? "unknown document"}
                {evidence.document_version ? ` v${evidence.document_version}` : ""}
                {evidence.section ? ` · ${evidence.section}` : ""}
                {evidence.page ? ` · page ${evidence.page}` : ""}
              </span>{" "}
              <FlagBadges flags={evidence.flags} />
              <span className="quote">{evidence.text}</span>
            </>
          )}
        </span>
      )}
    </span>
  );
}

/** Evidence references stored on a test case: document, section and the verbatim quote. */
export function EvidenceRefs({ refs }: { refs: EvidenceRef[] | null | undefined }) {
  if (!refs || refs.length === 0) return <span className="muted">no linked evidence</span>;
  return (
    <ul className="plain-list">
      {refs.map((ref, index) => (
        <li key={index}>
          <span className="muted small">
            {ref.filename ?? "document"}
            {ref.document_version ? ` v${ref.document_version}` : ""}
            {ref.section ? ` · ${ref.section}` : ""}
            {ref.page ? ` · page ${ref.page}` : ""}
          </span>
          {ref.quote && <span className="quote">{ref.quote}</span>}
        </li>
      ))}
    </ul>
  );
}
