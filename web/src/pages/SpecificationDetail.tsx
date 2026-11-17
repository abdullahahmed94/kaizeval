import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useSession } from "../auth";
import { EvidenceLink } from "../components/Evidence";
import {
  Badge, Card, Check, ErrorNote, Field, Loading, Notice, PageHeader, StatusBadge, SyntheticBanner, TableWrap, Tabs, When,
} from "../components/ui";
import { formatPercent, humanize } from "../format";
import { useAction, useLoad, useNames } from "../hooks";
import type { Dict, SpecContent, SpecObjective, Specification } from "../types";

function lines(text: string): string[] {
  return text.split("\n").map((line) => line.trim()).filter(Boolean);
}

function StringList({ items, empty }: { items: string[] | undefined; empty: string }) {
  if (!items || items.length === 0) return <p className="muted">{empty}</p>;
  return (
    <ul>
      {items.map((item, index) => (
        <li key={index}>{item}</li>
      ))}
    </ul>
  );
}

interface ObjectiveDraft {
  original: SpecObjective;
  statement: string;
  source: string;
  confidence: string;
  criteria: string;
}

function toDraft(objective: SpecObjective): ObjectiveDraft {
  return {
    original: objective, statement: objective.statement, source: objective.source,
    confidence: objective.confidence === undefined ? "" : String(objective.confidence),
    criteria: (objective.acceptance_criteria ?? []).join("\n"),
  };
}

function Editor({ spec, onSaved, onCancel }: { spec: Specification; onSaved: (created: Specification) => void; onCancel: () => void }) {
  const { api } = useSession();
  const [mode, setMode] = useState<"fields" | "json">("fields");
  const [purpose, setPurpose] = useState(spec.content.purpose ?? "");
  const [objectives, setObjectives] = useState<ObjectiveDraft[]>((spec.content.objectives ?? []).map(toDraft));
  const [constraints, setConstraints] = useState((spec.content.constraints ?? []).join("\n"));
  const [json, setJson] = useState(JSON.stringify(spec.content, null, 2));
  const [approve, setApprove] = useState(false);
  const [localError, setLocalError] = useState<string | null>(null);
  const action = useAction();

  const update = (index: number, change: Partial<ObjectiveDraft>) =>
    setObjectives((current) => current.map((item, position) => (position === index ? { ...item, ...change } : item)));

  const structuredContent = (): Dict => ({
    purpose: purpose.trim(),
    objectives: objectives.filter((item) => item.statement.trim()).map((item, index) => {
      const confidence = Number(item.confidence);
      return {
        ...item.original,
        id: item.original.id ?? `E${index + 1}`,
        statement: item.statement.trim(),
        source: item.source,
        confidence: item.confidence.trim() && Number.isFinite(confidence) ? confidence : item.original.confidence ?? 1,
        acceptance_criteria: lines(item.criteria),
        evidence_refs: item.original.evidence_refs ?? [],
      };
    }),
    constraints: lines(constraints),
  });

  const switchMode = (next: "fields" | "json") => {
    setLocalError(null);
    if (next === "json") setJson(JSON.stringify({ ...spec.content, ...structuredContent() }, null, 2));
    setMode(next);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setLocalError(null);
    let content: Dict;
    if (mode === "json") {
      try {
        const parsed: unknown = JSON.parse(json);
        if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not an object");
        content = parsed as Dict;
      } catch {
        setLocalError("The content must be a valid JSON object.");
        return;
      }
    } else {
      content = structuredContent();
    }
    void action.run(async () => onSaved(await api.editSpecification(spec.id, content, approve)));
  };

  return (
    <Card title="Review & Edit">
      <p className="muted">
        Saving never overwrites: it creates version {Math.max(...(spec.versions ?? [spec]).map((item) => item.version)) + 1} of
        this specification. Fields you do not change are carried over.
      </p>
      <Tabs tabs={[{ id: "fields", label: "Structured fields" }, { id: "json", label: "JSON" }]} current={mode} onChange={switchMode} />
      <form onSubmit={submit} className="form">
        {mode === "fields" ? (
          <>
            <Field label="Purpose">
              <textarea rows={2} value={purpose} onChange={(event) => setPurpose(event.target.value)} />
            </Field>
            <fieldset className="fieldset">
              <legend>Objectives</legend>
              {objectives.length === 0 && <p className="muted">No objectives. Add one below.</p>}
              {objectives.map((objective, index) => (
                <div key={index} className="box objective-edit">
                  <div className="form-grid">
                    <Field label={`Objective ${objective.original.id ?? index + 1}`}>
                      <textarea rows={2} value={objective.statement} onChange={(event) => update(index, { statement: event.target.value })} />
                    </Field>
                    <Field label="Acceptance criteria" hint="One per line.">
                      <textarea rows={2} value={objective.criteria} onChange={(event) => update(index, { criteria: event.target.value })} />
                    </Field>
                    <Field label="Source">
                      <select value={objective.source} onChange={(event) => update(index, { source: event.target.value })}>
                        <option value="requirement">requirement</option>
                        <option value="inferred">inferred</option>
                      </select>
                    </Field>
                    <Field label="Confidence (0 to 1)">
                      <input type="number" min={0} max={1} step={0.05} value={objective.confidence}
                        onChange={(event) => update(index, { confidence: event.target.value })} />
                    </Field>
                  </div>
                  <button type="button" className="btn btn-small" onClick={() => setObjectives((current) => current.filter((_, position) => position !== index))}>
                    Remove objective
                  </button>
                </div>
              ))}
              <button type="button" className="btn btn-small"
                onClick={() => setObjectives((current) => [...current, toDraft({ statement: "", source: "requirement", confidence: 1 })])}>
                Add objective
              </button>
            </fieldset>
            <Field label="Constraints" hint="One per line.">
              <textarea rows={4} value={constraints} onChange={(event) => setConstraints(event.target.value)} />
            </Field>
          </>
        ) : (
          <Field label="Specification content (JSON)" hint="Top-level keys you include replace the same keys of the current version.">
            <textarea rows={22} className="mono" value={json} onChange={(event) => setJson(event.target.value)} spellCheck={false} />
          </Field>
        )}
        <Check label="Approve the new version when saving" checked={approve} onChange={setApprove}
          hint="Records your approval and removes the provisional status." />
        {localError && <p className="error-text" role="alert">{localError}</p>}
        <ErrorNote error={action.error} />
        <div className="button-row">
          <button type="submit" className="btn btn-primary" disabled={action.busy}>
            {action.busy ? "Saving…" : approve ? "Save as new version and approve" : "Save as new version"}
          </button>
          <button type="button" className="btn" onClick={onCancel} disabled={action.busy}>
            Cancel
          </button>
        </div>
      </form>
    </Card>
  );
}

function Content({ content }: { content: SpecContent }) {
  const objectives = content.objectives ?? [];
  return (
    <>
      <Card title="Purpose">
        <p>{content.purpose || <span className="muted">No purpose stated.</span>}</p>
        {content.languages && content.languages.length > 0 && <p className="muted small">Languages: {content.languages.join(", ")}</p>}
      </Card>
      <Card title={`Objectives (${objectives.length})`}>
        {objectives.length === 0 && <p className="muted">No objectives.</p>}
        <ol className="objectives">
          {objectives.map((objective, index) => (
            <li key={objective.id ?? index} className="objective">
              <p>
                <strong>{objective.id ? `${objective.id}. ` : ""}{objective.statement}</strong>{" "}
                <Badge tone={objective.source === "requirement" ? "ok" : "warn"}>
                  {objective.source === "requirement" ? "requirement" : "inferred"}
                </Badge>{" "}
                <span className="muted small">confidence {formatPercent(objective.confidence)}</span>
              </p>
              {objective.acceptance_criteria && objective.acceptance_criteria.length > 0 ? (
                <>
                  <p className="muted small">Acceptance criteria</p>
                  <ul>
                    {objective.acceptance_criteria.map((criterion, position) => (
                      <li key={position}>{criterion}</li>
                    ))}
                  </ul>
                </>
              ) : (
                <p className="muted small">No acceptance criteria.</p>
              )}
              <p className="muted small">
                Evidence:{" "}
                {objective.evidence_refs && objective.evidence_refs.length > 0
                  ? objective.evidence_refs.map((reference) => <EvidenceLink key={reference} reference={reference} />)
                  : "none linked"}
              </p>
            </li>
          ))}
        </ol>
      </Card>
      <div className="two-col">
        <Card title="Constraints">
          <StringList items={content.constraints} empty="No constraints." />
        </Card>
        <Card title="Assumptions">
          <StringList items={content.assumptions} empty="No assumptions recorded." />
        </Card>
      </div>
      <Card title="Open questions">
        <StringList items={content.open_questions} empty="No open questions." />
      </Card>
    </>
  );
}

export function SpecificationDetailPage() {
  const { id = "" } = useParams();
  const { api, can, userLabel } = useSession();
  const navigate = useNavigate();
  const names = useNames();
  const spec = useLoad(() => api.specification(id), [api, id]);
  const [editing, setEditing] = useState(false);
  const [note, setNote] = useState("");
  const review = useAction();
  const data = spec.data;
  const reviewer = can("reviewer");

  const decide = (decision: "approve" | "skip") =>
    void review.run(async () => {
      await api.reviewSpecification(id, decision, note.trim() || null);
      setNote("");
      spec.reload();
    });

  return (
    <>
      <PageHeader title={data ? `Specification, version ${data.version}` : "Specification"}
        subtitle={<Link to="/specifications">All specifications</Link>} />
      {spec.loading && !data && <Loading />}
      <ErrorNote error={spec.error} />
      {data && (
        <>
          <SyntheticBanner show={data.synthetic} detail="This specification was not produced by a language model." />
          <Card title="Status">
            <dl className="kv">
              <dt>Status</dt>
              <dd>
                <StatusBadge status={data.status} />{" "}
                {data.content.provisional ? <Badge tone="warn">Provisional</Badge> : <Badge tone="ok">Not provisional</Badge>}
              </dd>
              <dt>Source</dt>
              <dd>{humanize(data.source)}</dd>
              <dt>Agent</dt>
              <dd>{data.agent_id ? <Link to={`/agents/${data.agent_id}`}>{names.agentName(data.agent_id)}</Link> : "—"}</dd>
              <dt>Created</dt>
              <dd><When value={data.created_at} /> by {userLabel(data.created_by)}</dd>
              <dt>Recorded choice</dt>
              <dd>
                {data.review ? (
                  <>
                    <strong>{data.review.decision === "approve" ? "Approved" : "Skip & Continue"}</strong> by{" "}
                    {userLabel(data.review.by)} on <When value={data.review.at} />
                    {data.review.note ? ` — "${data.review.note}"` : ""}
                  </>
                ) : (
                  <span className="muted">No choice has been recorded for this version.</span>
                )}
              </dd>
            </dl>
            {data.content.uncertainty && data.content.uncertainty.length > 0 && (
              <Notice tone="warn" title="Uncertainty">
                <ul>
                  {data.content.uncertainty.map((item, index) => (
                    <li key={index}>{item}</li>
                  ))}
                </ul>
              </Notice>
            )}

            {reviewer ? (
              <div className="review-actions">
                <h3>Your decision</h3>
                <p className="muted small">
                  Review & Edit saves your changes as a new version. Approve confirms this version as it is. Skip & Continue
                  lets work proceed with this version while keeping it marked as not reviewed.
                </p>
                <Field label="Note (optional, recorded with Approve or Skip & Continue)">
                  <input type="text" value={note} onChange={(event) => setNote(event.target.value)} maxLength={2000} />
                </Field>
                <div className="button-row">
                  <button type="button" className="btn" onClick={() => setEditing(true)} disabled={editing}>
                    Review & Edit
                  </button>
                  <button type="button" className="btn btn-primary" disabled={review.busy} onClick={() => decide("approve")}>
                    Approve
                  </button>
                  <button type="button" className="btn" disabled={review.busy} onClick={() => decide("skip")}>
                    Skip & Continue
                  </button>
                </div>
                <ErrorNote error={review.error} />
              </div>
            ) : (
              <p className="muted small">Reviewing a specification needs the reviewer role or higher.</p>
            )}
          </Card>

          {editing && (
            <Editor key={data.id} spec={data} onCancel={() => setEditing(false)}
              onSaved={(created) => { setEditing(false); navigate(`/specifications/${created.id}`); }} />
          )}

          <Content content={data.content} />

          <Card title="Version history">
            <TableWrap>
              <table className="data">
                <thead>
                  <tr>
                    <th scope="col">Version</th>
                    <th scope="col">Status</th>
                    <th scope="col">Source</th>
                    <th scope="col">Created</th>
                    <th scope="col">By</th>
                  </tr>
                </thead>
                <tbody>
                  {(data.versions ?? []).map((version) => (
                    <tr key={version.id} className={version.id === data.id ? "row-strong" : undefined}>
                      <th scope="row">
                        {version.id === data.id ? `Version ${version.version} (shown)` : (
                          <Link to={`/specifications/${version.id}`}>Version {version.version}</Link>
                        )}
                      </th>
                      <td><StatusBadge status={version.status} /></td>
                      <td>{humanize(version.source)}</td>
                      <td><When value={version.created_at} /></td>
                      <td>{userLabel(version.created_by)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
          </Card>
        </>
      )}
    </>
  );
}
