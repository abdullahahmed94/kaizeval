import { Fragment, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { useSession } from "../auth";
import { JobProgress } from "../components/JobProgress";
import {
  Card, Check, Empty, ErrorNote, Field, Loading, Notice, PageHeader, Pager, StatusBadge, SyntheticBadge, TableWrap, When,
} from "../components/ui";
import { formatPercent, humanize, parseOptionalNumber } from "../format";
import { useAction, useLoad, useNames } from "../hooks";
import type { Dict, StartRunResult } from "../types";

const PAGE = 25;

export const EVALUATION_MODES: { id: string; label: string; explanation: string }[] = [
  { id: "deterministic", label: "Rules only (no reviewers)",
    explanation: "Rule-based checks only (key facts, citations, refusals). Needs no model; dimensions that need judgment are not scored." },
  { id: "single", label: "1 reviewer",
    explanation: "Rule-based checks first, then one model reviewer for what rules cannot decide. Reviewer scores are estimates." },
  { id: "dual", label: "2 reviewers",
    explanation: "Two independent model reviewers, with an arbiter when they disagree or a critical case is in doubt. Uses more model calls." },
  { id: "panel", label: "Reviewer panel (3 to 7)",
    explanation: "A panel of independent model reviewers; their scores are averaged when they agree and an arbiter decides when they do not. Agreement is still not proof." },
];

function StartForm({ onStarted }: { onStarted: () => void }) {
  const { api } = useSession();
  const names = useNames();
  const [agentId, setAgentId] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [mode, setMode] = useState("single");
  const [reviewers, setReviewers] = useState("3");
  const [testers, setTesters] = useState("1");
  const [maxAgentCalls, setMaxAgentCalls] = useState("");
  const [maxModelCalls, setMaxModelCalls] = useState("");
  const [started, setStarted] = useState<StartRunResult | null>(null);
  const start = useAction();
  const cancel = useAction();

  const readyDatasets = names.datasets.filter((dataset) => dataset.status === "ready");
  const agent = names.agents.find((item) => item.id === agentId);
  const explanation = EVALUATION_MODES.find((item) => item.id === mode)?.explanation;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const limits: Dict<number> = {};
    const agentCalls = parseOptionalNumber(maxAgentCalls);
    const modelCalls = parseOptionalNumber(maxModelCalls);
    if (agentCalls !== null) limits.max_agent_calls = agentCalls;
    if (modelCalls !== null) limits.max_model_calls = modelCalls;
    void start.run(async () => {
      const result = await api.startRun({
        agent_id: agentId, dataset_id: datasetId, mode, split: "regression", agent_version: "baseline",
        config: {
          ...(Object.keys(limits).length ? { limits } : {}),
          ...(mode === "panel" ? { reviewers: Math.min(7, Math.max(2, Number(reviewers) || 3)) } : {}),
          testers: Math.min(8, Math.max(1, Number(testers) || 1)),
        },
      });
      setStarted(result);
      onStarted();
    });
  };

  return (
    <Card title="Start an evaluation">
      <p className="muted">Runs the agent's current (baseline) version on the regression set of a dataset.</p>
      <ErrorNote error={names.error} />
      <form onSubmit={submit} className="form">
        <div className="form-grid">
          <Field label="Agent">
            <select value={agentId} onChange={(event) => setAgentId(event.target.value)} required>
              <option value="">Choose an agent</option>
              {names.agents.map((item) => (
                <option key={item.id} value={item.id}>{item.name}{item.verified ? "" : " (not verified)"}</option>
              ))}
            </select>
          </Field>
          <Field label="Dataset">
            <select value={datasetId} onChange={(event) => setDatasetId(event.target.value)} required>
              <option value="">Choose a dataset</option>
              {readyDatasets.map((item) => (
                <option key={item.id} value={item.id}>{item.name} ({item.counts.regression ?? 0} regression cases)</option>
              ))}
            </select>
          </Field>
          <Field label="Evaluation mode" hint={explanation}>
            <select value={mode} onChange={(event) => setMode(event.target.value)}>
              {EVALUATION_MODES.map((item) => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </select>
          </Field>
          {mode === "panel" && (
            <Field label="Number of reviewers" hint="Between 2 and 7 independent reviewers for every answer.">
              <input type="number" value={reviewers} min={2} max={7} step={1} onChange={(event) => setReviewers(event.target.value)} required />
            </Field>
          )}
          <Field label="Number of testers" hint="Between 1 and 8. Testers call the agent in parallel; reviewers score only after the answers are in.">
            <input type="number" value={testers} min={1} max={8} step={1} onChange={(event) => setTesters(event.target.value)} required />
          </Field>
          <Field label="Max agent calls (optional)" hint="The run stops when this many calls to the agent would be exceeded.">
            <input type="number" value={maxAgentCalls} min={1} step={1} onChange={(event) => setMaxAgentCalls(event.target.value)} />
          </Field>
          <Field label="Max model calls (optional)" hint="Budget for judge calls to platform models.">
            <input type="number" value={maxModelCalls} min={1} step={1} onChange={(event) => setMaxModelCalls(event.target.value)} />
          </Field>
        </div>
        {agent && !agent.verified && (
          <Notice tone="warn">
            This agent has not been verified; the run will fail until you <Link to={`/agents/${agent.id}`}>verify its connection</Link>.
          </Notice>
        )}
        {names.agents.length === 0 && !names.loading && (
          <p className="muted small">No agent is registered. <Link to="/agents">Register one</Link> first.</p>
        )}
        {readyDatasets.length === 0 && !names.loading && (
          <p className="muted small">No dataset is ready. <Link to="/datasets">Generate or import one</Link> first.</p>
        )}
        <details>
          <summary>What the evaluation modes mean</summary>
          <dl className="kv">
            {EVALUATION_MODES.map((item) => (
              <Fragment key={item.id}>
                <dt>{item.label}</dt>
                <dd>{item.explanation}</dd>
              </Fragment>
            ))}
          </dl>
        </details>
        <ErrorNote error={start.error} />
        <div className="button-row">
          <button type="submit" className="btn btn-primary" disabled={start.busy || !agentId || !datasetId}>
            {start.busy ? "Starting…" : "Start evaluation"}
          </button>
        </div>
      </form>
      {started && (
        <div className="box">
          <JobProgress key={started.job.id} jobId={started.job.id} label="Evaluation" onSettled={onStarted}>
            <button type="button" className="btn btn-small btn-danger" disabled={cancel.busy}
              onClick={() => void cancel.run(async () => { await api.cancelRun(started.run.id); })}>
              {cancel.busy ? "Cancelling…" : "Cancel"}
            </button>
          </JobProgress>
          <ErrorNote error={cancel.error} />
          <p>
            <Link to={`/evaluations/${started.run.id}`}>Open this run</Link>
          </p>
        </div>
      )}
    </Card>
  );
}

export function EvaluationsPage() {
  const { api, can } = useSession();
  const names = useNames();
  const [offset, setOffset] = useState(0);
  const [standalone, setStandalone] = useState(false);
  const runs = useLoad(() => api.runs({ limit: PAGE, offset, standalone }), [api, offset, standalone]);
  const data = runs.data;

  return (
    <>
      <PageHeader title="Evaluations" subtitle="Run the agent on a regression set, score every answer and keep the evidence." />
      {can("editor") ? <StartForm onStarted={runs.reload} /> : (
        <p className="muted small">Only editors and owners can start an evaluation.</p>
      )}
      <Card title="Run history" actions={<button type="button" className="btn btn-small" onClick={runs.reload}>Refresh</button>}>
        <div className="filters">
          <Check label="Hide runs that belong to an optimization" checked={standalone}
            onChange={(value) => { setStandalone(value); setOffset(0); }} />
        </div>
        {runs.loading && !data && <Loading />}
        <ErrorNote error={runs.error} />
        {data && data.items.length === 0 && (
          <Empty title="No evaluation run yet.">
            {can("editor") ? "Start one above once an agent is verified and a dataset is ready." : "An editor needs to start an evaluation."}
          </Empty>
        )}
        {data && data.items.length > 0 && (
          <TableWrap>
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Started</th>
                  <th scope="col">Agent</th>
                  <th scope="col">Dataset</th>
                  <th scope="col">Set</th>
                  <th scope="col">Version</th>
                  <th scope="col">Mode</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">Overall</th>
                  <th scope="col" className="num">Pass rate</th>
                  <th scope="col" className="num">Critical failures</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((run) => (
                  <tr key={run.id}>
                    <th scope="row">
                      <Link to={`/evaluations/${run.id}`}><When value={run.created_at} /></Link>{" "}
                      {run.synthetic && <SyntheticBadge />}
                    </th>
                    <td>{names.agentName(run.agent_id)}</td>
                    <td>{names.datasetName(run.dataset_id)}</td>
                    <td>
                      {humanize(run.split)}
                      {run.optimization_run_id && (
                        <span className="muted small"> · <Link to={`/optimizations/${run.optimization_run_id}`}>optimization</Link></span>
                      )}
                    </td>
                    <td>{run.agent_version}</td>
                    <td>{run.mode}</td>
                    <td><StatusBadge status={run.status} /></td>
                    <td className="num">{formatPercent(run.summary?.overall)}</td>
                    <td className="num">{formatPercent(run.summary?.pass_rate)}</td>
                    <td className="num">{run.summary ? run.summary.critical_failures : "—"}</td>
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
