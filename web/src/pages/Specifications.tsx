import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useSession } from "../auth";
import { JobProgress } from "../components/JobProgress";
import {
  Card, Check, Empty, ErrorNote, Field, Loading, PageHeader, Pager, StatusBadge, SyntheticBadge, TableWrap, When,
} from "../components/ui";
import { humanize } from "../format";
import { useAction, useLoad, useNames } from "../hooks";
import type { Job } from "../types";

const PAGE = 50;

function BuildForm({ onBuilt }: { onBuilt: () => void }) {
  const { api } = useSession();
  const navigate = useNavigate();
  const names = useNames();
  const requirements = useLoad(() => api.documents({ kind: "requirements", limit: 200 }), [api]);
  const [agentId, setAgentId] = useState("");
  const [text, setText] = useState("");
  const [documentIds, setDocumentIds] = useState<string[]>([]);
  const [infer, setInfer] = useState(true);
  const [job, setJob] = useState<Job | null>(null);
  const action = useAction();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void action.run(async () => {
      setJob(await api.buildSpecification({
        agent_id: agentId || null, requirements_text: text.trim() || null,
        requirement_document_ids: documentIds, infer,
      }));
    });
  };

  const settled = (finished: Job) => {
    onBuilt();
    const specificationId = finished.result?.specification_id;
    if (finished.status === "completed" && typeof specificationId === "string") navigate(`/specifications/${specificationId}`);
  };

  const parsedRequirements = requirements.data?.items.filter((document) => document.status === "parsed") ?? [];

  return (
    <Card title="Build a specification">
      <p className="muted">
        A specification states what the agent is for. It can come from your business requirements, be inferred from the
        knowledge base and the agent's readable configuration, or both. Inferred objectives stay provisional until a person reviews them.
      </p>
      <form onSubmit={submit} className="form">
        <div className="form-grid">
          <Field label="Agent (optional)" hint="Used to read the agent's description when its configuration is readable.">
            <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
              <option value="">No agent</option>
              {names.agents.map((agent) => (
                <option key={agent.id} value={agent.id}>{agent.name}</option>
              ))}
            </select>
          </Field>
        </div>
        <Field label="Business requirements (optional)" hint="One requirement per line works well.">
          <textarea rows={5} value={text} onChange={(event) => setText(event.target.value)} maxLength={50000} />
        </Field>
        <fieldset className="fieldset">
          <legend>Requirement documents (optional)</legend>
          <ErrorNote error={requirements.error} />
          {parsedRequirements.length === 0 ? (
            <p className="muted small">
              No requirement document has been uploaded. Upload one on the <Link to="/knowledge">Knowledge</Link> page with the kind
              "Business requirements".
            </p>
          ) : (
            parsedRequirements.map((document) => (
              <Check key={document.id} label={`${document.filename} (version ${document.version})`}
                checked={documentIds.includes(document.id)}
                onChange={(checked) => setDocumentIds((current) =>
                  checked ? [...current, document.id] : current.filter((item) => item !== document.id))} />
            ))
          )}
        </fieldset>
        <Check label="Infer objectives from the knowledge base and the agent" checked={infer} onChange={setInfer}
          hint="Needs a verified model for the specification inference role. Without inference, requirements are required." />
        <ErrorNote error={action.error} />
        <div className="button-row">
          <button type="submit" className="btn btn-primary" disabled={action.busy}>
            {action.busy ? "Starting…" : "Build specification"}
          </button>
        </div>
      </form>
      {job && <JobProgress key={job.id} jobId={job.id} label="Building the specification" onSettled={settled} />}
    </Card>
  );
}

export function SpecificationsPage() {
  const { api, can, userLabel } = useSession();
  const names = useNames();
  const [offset, setOffset] = useState(0);
  const specs = useLoad(() => api.specifications({ limit: PAGE, offset }), [api, offset]);
  const data = specs.data;

  return (
    <>
      <PageHeader title="Specification" subtitle="What the agent is supposed to do, as a versioned and reviewable document." />
      {can("editor") ? <BuildForm onBuilt={specs.reload} /> : (
        <p className="muted small">Only editors and owners can build a specification.</p>
      )}
      <Card title="Specifications" actions={<button type="button" className="btn btn-small" onClick={specs.reload}>Refresh</button>}>
        {specs.loading && !data && <Loading />}
        <ErrorNote error={specs.error} />
        {data && data.items.length === 0 && (
          <Empty title="No specification yet.">
            {can("editor") ? "Build one above from your requirements, from inference, or both."
              : "An editor needs to build a specification."}
          </Empty>
        )}
        {data && data.items.length > 0 && (
          <TableWrap>
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Specification</th>
                  <th scope="col">Agent</th>
                  <th scope="col">Source</th>
                  <th scope="col">Status</th>
                  <th scope="col">Recorded choice</th>
                  <th scope="col">Created</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((spec) => (
                  <tr key={spec.id}>
                    <th scope="row">
                      <Link to={`/specifications/${spec.id}`}>
                        Version {spec.version} · {spec.content.objectives?.length ?? 0} objective(s)
                      </Link>{" "}
                      {spec.synthetic && <SyntheticBadge />}
                    </th>
                    <td>{spec.agent_id ? names.agentName(spec.agent_id) : "—"}</td>
                    <td>{humanize(spec.source)}</td>
                    <td><StatusBadge status={spec.status} /></td>
                    <td>
                      {spec.review
                        ? `${spec.review.decision === "approve" ? "Approved" : "Skipped"} by ${userLabel(spec.review.by)}`
                        : <span className="muted">none yet</span>}
                    </td>
                    <td><When value={spec.created_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
        {data && <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />}
      </Card>
    </>
  );
}
