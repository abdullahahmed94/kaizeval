import { useEffect, useId, useRef, type ReactNode } from "react";

import { ApiError } from "../api";
import { formatDate, formatPercent, humanize } from "../format";
import type { Dict } from "../types";

// --- feedback -----------------------------------------------------------------------------

/** Shows an API error with its message and code, plus field issues for validation errors. */
export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const api = error instanceof ApiError ? error : null;
  const message = api ? api.message : error instanceof Error ? error.message : String(error);
  return (
    <div className="alert alert-error" role="alert">
      <p>
        {message} {api && <code className="error-code">{api.code}</code>}
      </p>
      {api && api.issues.length > 0 && (
        <ul>
          {api.issues.map((issue, index) => (
            <li key={index}>
              <code>{issue.field || "request"}</code>: {issue.message}
            </li>
          ))}
        </ul>
      )}
      {api?.status === 403 && <p className="small">Your role in this workspace does not allow this action.</p>}
      {api?.status === 404 && (
        <p className="small">It may belong to another workspace. Check the workspace selected in the menu.</p>
      )}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p className="loading" role="status">
      <span className="spinner" aria-hidden="true" /> {label}
    </p>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <p className="empty-title">{title}</p>
      {children && <div className="empty-body">{children}</div>}
    </div>
  );
}

export function Notice({ tone = "info", title, children }: { tone?: "info" | "warn" | "ok"; title?: string; children: ReactNode }) {
  return (
    <div className={`alert alert-${tone}`} role={tone === "warn" ? "alert" : "note"}>
      {title && <p className="alert-title">{title}</p>}
      {children}
    </div>
  );
}

// --- badges -------------------------------------------------------------------------------

export type Tone = "neutral" | "ok" | "warn" | "bad" | "info" | "synthetic";

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${tone}`} title={title}>
      {children}
    </span>
  );
}

const STATUS_TONES: Record<string, Tone> = {
  completed: "ok", ready: "ok", parsed: "ok", pass: "ok", kept: "ok", approved: "ok", reviewed: "ok", applied: "ok",
  promoted: "ok", improved: "ok", verified: "ok",
  running: "info", queued: "info", generating: "info", active: "info", proposed: "info", draft: "info",
  review_required: "warn", needs_human_review: "warn", awaiting_decision: "warn", deferred: "warn", skipped: "warn",
  inconclusive: "warn", superseded: "neutral", unverified: "warn", rolled_back: "warn", invalid: "warn",
  failed: "bad", fail: "bad", rejected: "bad", cancelled: "neutral", regressed: "bad", critical: "bad",
  high: "warn", medium: "warn", low: "neutral", normal: "neutral",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <Badge>—</Badge>;
  return <Badge tone={STATUS_TONES[status] ?? "neutral"}>{humanize(status)}</Badge>;
}

export const SYNTHETIC_TEXT = "SYNTHETIC — produced with the fake model provider; not a real evaluation";

export function SyntheticBadge() {
  return (
    <Badge tone="synthetic" title={SYNTHETIC_TEXT}>
      SYNTHETIC
    </Badge>
  );
}

export function SyntheticBanner({ show, detail }: { show: boolean | undefined; detail?: string | null }) {
  if (!show) return null;
  return (
    <div className="synthetic-banner" role="note">
      <strong>{SYNTHETIC_TEXT}.</strong>
      {detail ? ` ${detail}` : " Scores on this page exercise the workflow only and say nothing about real quality."}
    </div>
  );
}

// --- layout pieces ------------------------------------------------------------------------

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="page-header">
      <div>
        <h1>{title}</h1>
        {subtitle && <p className="muted">{subtitle}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function Card({ title, actions, children, id }: { title?: ReactNode; actions?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <section className="card" id={id}>
      {(title || actions) && (
        <div className="card-header">
          {title && <h2>{title}</h2>}
          {actions && <div className="card-actions">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  );
}

export function Check({ label, checked, onChange, disabled, hint }: {
  label: ReactNode; checked: boolean; onChange: (value: boolean) => void; disabled?: boolean; hint?: ReactNode;
}) {
  return (
    <label className="check">
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onChange(event.target.checked)} />
      <span>
        {label}
        {hint && <span className="field-hint">{hint}</span>}
      </span>
    </label>
  );
}

export function TableWrap({ children }: { children: ReactNode }) {
  return <div className="table-wrap">{children}</div>;
}

export function Stat({ label, value, note }: { label: string; value: ReactNode; note?: ReactNode }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {note && <span className="stat-note">{note}</span>}
    </div>
  );
}

export function When({ value }: { value: string | null | undefined }) {
  return <time dateTime={value ?? undefined}>{formatDate(value)}</time>;
}

// --- data display -------------------------------------------------------------------------

export function Pager({ total, limit, offset, onChange }: {
  total: number; limit: number; offset: number; onChange: (offset: number) => void;
}) {
  if (total <= limit && offset === 0) return null;
  const from = total === 0 ? 0 : offset + 1;
  const to = Math.min(total, offset + limit);
  return (
    <nav className="pager" aria-label="Pagination">
      <button type="button" className="btn btn-small" disabled={offset <= 0} onClick={() => onChange(Math.max(0, offset - limit))}>
        Previous
      </button>
      <span className="muted">
        {from}–{to} of {total}
      </span>
      <button type="button" className="btn btn-small" disabled={to >= total} onClick={() => onChange(offset + limit)}>
        Next
      </button>
    </nav>
  );
}

export function JsonBlock({ value }: { value: unknown }) {
  return <pre className="code">{JSON.stringify(value, null, 2)}</pre>;
}

/** Renders the key-level diff text produced by the API ("- key: old" / "+ key: new"). */
export function DiffBlock({ diff }: { diff: string | null | undefined }) {
  const lines = (diff || "(no changes)").split("\n");
  return (
    <pre className="code diff" aria-label="Configuration diff">
      {lines.map((line, index) => (
        <span key={index} className={line.startsWith("+") ? "diff-add" : line.startsWith("-") ? "diff-del" : undefined}>
          {line}
          {"\n"}
        </span>
      ))}
    </pre>
  );
}

/** A horizontal bar for a 0..1 score. The number is always printed next to it. */
export function ScoreBar({ value, tone = "accent" }: { value: number | null | undefined; tone?: "accent" | "muted" }) {
  const width = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value)) * 100;
  return (
    <span className="bar" aria-hidden="true">
      <span className={`bar-fill bar-${tone}`} style={{ width: `${width}%` }} />
    </span>
  );
}

export function ScoreCell({ value }: { value: number | null | undefined }) {
  return (
    <span className="score-cell">
      <ScoreBar value={value} />
      <span className="score-number">{formatPercent(value)}</span>
    </span>
  );
}

/** Counts keyed by a label, e.g. cases by category. */
export function CountList({ counts, empty = "none", raw = false }: {
  counts: Dict<number> | null | undefined; empty?: string;
  /** Show keys exactly as stored (file names, language codes) instead of humanizing them. */
  raw?: boolean;
}) {
  const entries = Object.entries(counts ?? {});
  if (entries.length === 0) return <span className="muted">{empty}</span>;
  return (
    <ul className="count-list">
      {entries.map(([key, count]) => (
        <li key={key}>
          <span>{raw ? key : humanize(key)}</span>
          <strong>{count}</strong>
        </li>
      ))}
    </ul>
  );
}

export function Tabs<T extends string>({ tabs, current, onChange }: {
  tabs: { id: T; label: ReactNode }[]; current: T; onChange: (id: T) => void;
}) {
  return (
    <div className="tabs" role="tablist">
      {tabs.map((tab) => (
        <button key={tab.id} type="button" role="tab" aria-selected={tab.id === current}
          className={tab.id === current ? "tab tab-active" : "tab"} onClick={() => onChange(tab.id)}>
          {tab.label}
        </button>
      ))}
    </div>
  );
}

// --- dialog -------------------------------------------------------------------------------

export function ConfirmDialog({ open, title, children, confirmLabel, danger, busy, onConfirm, onCancel }: {
  open: boolean; title: string; children: ReactNode; confirmLabel: string; danger?: boolean; busy?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog ref={ref} className="dialog" aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); onCancel(); }}>
      <h2 id={titleId}>{title}</h2>
      <div className="dialog-body">{children}</div>
      <div className="dialog-actions">
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="button" className={danger ? "btn btn-danger" : "btn btn-primary"} onClick={onConfirm} disabled={busy}>
          {busy ? "Working…" : confirmLabel}
        </button>
      </div>
    </dialog>
  );
}

/** Saves text the page already holds as a file (used for report downloads; no network involved). */
export function saveTextFile(filename: string, text: string, mime: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
