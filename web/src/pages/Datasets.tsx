import { useRef, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useSession } from "../auth";
import {
  Badge, Card, Check, Empty, ErrorNote, Field, Loading, Notice, PageHeader, Pager, StatusBadge, SyntheticBadge, TableWrap, When,
} from "../components/ui";
import { formatPercent } from "../format";
import { useAction, useLoad, useNames } from "../hooks";
import type { Dataset, ImportDatasetResult } from "../types";

const PAGE = 50;
const CATEGORIES: { id: string; label: string }[] = [
  { id: "knowledge", label: "Knowledge questions" },
  { id: "citation", label: "Citation checks" },
  { id: "edge_case", label: "Edge cases" },
];

function GenerateForm() {
  const { api } = useSession();
  const navigate = useNavigate();
  const names = useNames();
  const specs = useLoad(() => api.specifications({ limit: 100 }), [api]);
  const [name, setName] = useState("");
  const [agentId, setAgentId] = useState("");
  const [specId, setSpecId] = useState("");
  const [nCases, setNCases] = useState("20");
  const [holdout, setHoldout] = useState("30");
  const [seed, setSeed] = useState("0");
  const [categories, setCategories] = useState<string[]>(CATEGORIES.map((item) => item.id));
  const [multiTurn, setMultiTurn] = useState(true);
  const [unanswerable, setUnanswerable] = useState("20");
  const [languages, setLanguages] = useState("");
  const action = useAction();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void action.run(async () => {
      const result = await api.generateDataset({
        name: name.trim(), agent_id: agentId || null, specification_id: specId || null,
        n_cases: Number(nCases), holdout_fraction: Number(holdout) / 100, seed: Number(seed) || 0,
        categories, include_multi_turn: multiTurn, unanswerable_share: Number(unanswerable) / 100,
        languages: languages.split(",").map((item) => item.trim()).filter(Boolean),
      });
      navigate(`/datasets/${result.dataset.id}?job=${result.job.id}`);
    });
  };

  return (
    <Card title="Generate a dataset from the knowledge base">
      <p className="muted">
        Questions are generated from uploaded knowledge documents and linked to the passage that answers them. The cases are split
        into a regression set and a sealed holdout before any optimization. Needs a verified model for the test generator and
        reference draft roles.
      </p>
      <form onSubmit={submit} className="form">
        <div className="form-grid">
          <Field label="Name">
            <input type="text" value={name} onChange={(event) => setName(event.target.value)} required maxLength={200} />
          </Field>
          <Field label="Agent (optional)">
            <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
              <option value="">No agent</option>
              {names.agents.map((agent) => (
                <option key={agent.id} value={agent.id}>{agent.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Specification (optional)">
            <select value={specId} onChange={(event) => setSpecId(event.target.value)}>
              <option value="">No specification</option>
              {(specs.data?.items ?? []).map((spec) => (
                <option key={spec.id} value={spec.id}>
                  Version {spec.version} ({spec.status}){spec.agent_id ? ` · ${names.agentName(spec.agent_id)}` : ""}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Number of cases" hint="Between 4 and 500.">
            <input type="number" value={nCases} min={4} max={500} step={1} onChange={(event) => setNCases(event.target.value)} required />
          </Field>
          <Field label="Holdout share (%)" hint="0 to 60. Kept sealed for the final, independent check.">
            <input type="number" value={holdout} min={0} max={60} step={1} onChange={(event) => setHoldout(event.target.value)} required />
          </Field>
          <Field label="Unanswerable share (%)" hint="0 to 50. Questions the agent should decline.">
            <input type="number" value={unanswerable} min={0} max={50} step={1} onChange={(event) => setUnanswerable(event.target.value)} required />
          </Field>
          <Field label="Seed" hint="Same seed, same split.">
            <input type="number" value={seed} step={1} onChange={(event) => setSeed(event.target.value)} />
          </Field>
          <Field label="Languages (optional)" hint="Comma-separated, for example fr, de. Adds translated variants of some questions.">
            <input type="text" value={languages} onChange={(event) => setLanguages(event.target.value)} />
          </Field>
        </div>
        <fieldset className="fieldset">
          <legend>Categories</legend>
          {CATEGORIES.map((category) => (
            <Check key={category.id} label={category.label} checked={categories.includes(category.id)}
              onChange={(checked) => setCategories((current) =>
                checked ? [...current, category.id] : current.filter((item) => item !== category.id))} />
          ))}
          <Check label="Include multi-turn conversations" checked={multiTurn} onChange={setMultiTurn} />
        </fieldset>
        <ErrorNote error={specs.error} />
        <ErrorNote error={action.error} />
        <div className="button-row">
          <button type="submit" className="btn btn-primary" disabled={action.busy || categories.length === 0}>
            {action.busy ? "Starting…" : "Generate dataset"}
          </button>
        </div>
      </form>
    </Card>
  );
}

function ImportForm({ datasets, onImported }: { datasets: Dataset[]; onImported: () => void }) {
  const { api } = useSession();
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [approved, setApproved] = useState(true);
  const [holdout, setHoldout] = useState("0");
  const [result, setResult] = useState<ImportDatasetResult | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const action = useAction();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!file) return;
    setResult(null);
    void action.run(async () => {
      setResult(await api.importDataset({
        file, name: name.trim() || undefined, datasetId: datasetId || undefined, approved,
        holdoutFraction: Number(holdout) / 100,
      }));
      setFile(null);
      if (input.current) input.current.value = "";
      onImported();
    });
  };

  return (
    <Card title="Import your own cases">
      <p className="muted">
        Import business-owned questions and answers or real conversations. Columns: <code>question</code> (required),{" "}
        <code>expected_answer</code>, <code>key_facts</code> (separated by semicolons), <code>expected_behavior</code>{" "}
        (answer or abstain), <code>category</code>, <code>severity</code>, <code>language</code>, <code>follow_up</code>,{" "}
        <code>split</code>.
      </p>
      <form onSubmit={submit} className="form">
        <div className="form-grid">
          <Field label="File" hint="CSV, JSONL or JSON.">
            <input ref={input} type="file" accept=".csv,.jsonl,.ndjson,.json" onChange={(event) => setFile(event.target.files?.[0] ?? null)} required />
          </Field>
          <Field label="Add to" hint="Create a new dataset or append to an existing one.">
            <select value={datasetId} onChange={(event) => setDatasetId(event.target.value)}>
              <option value="">A new dataset</option>
              {datasets.map((dataset) => (
                <option key={dataset.id} value={dataset.id}>{dataset.name}</option>
              ))}
            </select>
          </Field>
          {!datasetId && (
            <Field label="Name of the new dataset (optional)" hint="Defaults to the file name.">
              <input type="text" value={name} onChange={(event) => setName(event.target.value)} maxLength={200} />
            </Field>
          )}
          <Field label="Holdout share (%)" hint="0 to 60. Applied to rows without a split column.">
            <input type="number" value={holdout} min={0} max={60} step={1} onChange={(event) => setHoldout(event.target.value)} required />
          </Field>
        </div>
        <Check label="These reference answers are approved by the business" checked={approved} onChange={setApproved}
          hint="Approved references outrank generated drafts as ground truth. Leave unchecked to import them as drafts." />
        <ErrorNote error={action.error} />
        <div className="button-row">
          <button type="submit" className="btn" disabled={action.busy || !file}>
            {action.busy ? "Importing…" : "Import cases"}
          </button>
        </div>
      </form>
      {result && (
        <Notice tone="ok">
          <p>
            Imported {result.imported} case(s) into <Link to={`/datasets/${result.dataset.id}`}>{result.dataset.name}</Link>:{" "}
            {result.regression} regression, {result.holdout} holdout. {result.skipped} row(s) skipped (no question).
          </p>
        </Notice>
      )}
    </Card>
  );
}

export function DatasetsPage() {
  const { api, can } = useSession();
  const names = useNames();
  const [offset, setOffset] = useState(0);
  const datasets = useLoad(() => api.datasets({ limit: PAGE, offset }), [api, offset]);
  const data = datasets.data;

  return (
    <>
      <PageHeader title="Datasets" subtitle="Test cases with evidence-linked references, split into a regression set and a sealed holdout." />
      <Card title="Datasets" actions={<button type="button" className="btn btn-small" onClick={datasets.reload}>Refresh</button>}>
        {datasets.loading && !data && <Loading />}
        <ErrorNote error={datasets.error} />
        {data && data.items.length === 0 && (
          <Empty title="No dataset yet.">
            {can("editor") ? "Generate one from your knowledge documents or import your own cases below."
              : "An editor needs to generate or import a dataset."}
          </Empty>
        )}
        {data && data.items.length > 0 && (
          <TableWrap>
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Status</th>
                  <th scope="col">Agent</th>
                  <th scope="col" className="num">Regression</th>
                  <th scope="col" className="num">Holdout (sealed)</th>
                  <th scope="col">Holdout state</th>
                  <th scope="col" className="num">Version</th>
                  <th scope="col">Created</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((dataset) => (
                  <tr key={dataset.id}>
                    <th scope="row">
                      <Link to={`/datasets/${dataset.id}`}>{dataset.name}</Link> {dataset.synthetic && <SyntheticBadge />}
                    </th>
                    <td><StatusBadge status={dataset.status} /></td>
                    <td>{dataset.agent_id ? names.agentName(dataset.agent_id) : "—"}</td>
                    <td className="num">{dataset.counts.regression ?? 0}</td>
                    <td className="num">{dataset.counts.holdout ?? 0}</td>
                    <td>
                      {(dataset.counts.holdout ?? 0) === 0 ? <span className="muted">no holdout</span>
                        : dataset.holdout_exposed ? <Badge tone="warn">Exposed, rotate before reuse</Badge>
                        : <Badge tone="ok">Sealed, unused</Badge>}
                      <span className="muted small"> · {formatPercent(dataset.holdout_fraction)} target</span>
                    </td>
                    <td className="num">{dataset.version}</td>
                    <td><When value={dataset.created_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
        {data && <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />}
      </Card>
      {can("editor") ? (
        <>
          <GenerateForm />
          <ImportForm datasets={names.datasets} onImported={() => { datasets.reload(); names.reload(); }} />
        </>
      ) : (
        <p className="muted small">Only editors and owners can generate or import datasets.</p>
      )}
    </>
  );
}
