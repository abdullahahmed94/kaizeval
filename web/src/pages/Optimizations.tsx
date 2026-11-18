import { useState, type FormEvent, type ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError } from "../api";
import { useSession } from "../auth";
import {
  Card, Check, Empty, ErrorNote, Field, Loading, Notice, PageHeader, Pager, StatusBadge, SyntheticBadge, TableWrap, When,
} from "../components/ui";
import { formatPercent, formatPp, humanize, parseOptionalNumber } from "../format";
import { useAction, useLoad, useNames } from "../hooks";
import type { AgentCapabilities, Dict } from "../types";
import { EVALUATION_MODES } from "./Evaluations";

const PAGE = 25;
const NEEDED = ["inspect", "create_candidate", "apply_patch", "restore_candidate"];

/** Prominent explanation shown instead of a start button when optimization is refused. */
function Blocked({ code, message, children }: { code: string; message: string; children?: ReactNode }) {
  return (
    <div className="blocked" role="alert">
      <h3>Optimization cannot start</h3>
      <p>
        {message} <code className="error-code">{code}</code>
      </p>
      {children}
      <p>
        Evaluation and recommendations are not affected. <Link to="/recommendations">Open recommendations</Link> to see the
        proposed changes, which you can also apply with your own process.
      </p>
    </div>
  );
}

function CapabilityBlock({ capabilities }: { capabilities: AgentCapabilities }) {
  const missing = capabilities.matrix.filter((row) => NEEDED.includes(row.capability) && !row.available);
  const first = missing.find((row) => row.reason)?.reason ?? "";
  const code = first.includes(":") ? first.split(":")[0] : "UNSUPPORTED_CAPABILITY";
  return (
    <Blocked code={code}
      message={capabilities.explanation ?? "This agent does not provide the capabilities needed to patch an isolated candidate."}>
      <ul>
        {missing.map((row) => (
          <li key={row.capability}>
            <strong>{humanize(row.capability)}</strong>:{" "}
            {row.reason ?? (capabilities.verified ? "not confirmed by the last verification" : "the agent has not been verified yet")}
          </li>
        ))}
      </ul>
    </Blocked>
  );
}

function StartForm() {
  const { api, can, workspace } = useSession();
  const navigate = useNavigate();
  const names = useNames();
  const [agentId, setAgentId] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [mode, setMode] = useState("human_guided");
  const [maxRounds, setMaxRounds] = useState("");
  const [targetScore, setTargetScore] = useState("");
  const [stagnation, setStagnation] = useState("3");
  const [minImprovement, setMinImprovement] = useState("0.5");
  const [maxRegression, setMaxRegression] = useState("5");
  const [evalMode, setEvalMode] = useState("single");
  const [runHoldout, setRunHoldout] = useState(true);
  const [maxModelCalls, setMaxModelCalls] = useState("");
  const [maxModelTokens, setMaxModelTokens] = useState("");
  const [maxModelCost, setMaxModelCost] = useState("");
  const [maxAgentCalls, setMaxAgentCalls] = useState("");
  const start = useAction();

  const capabilities = useLoad(
    async () => (agentId ? api.agentCapabilities(agentId) : null),
    [api, agentId],
  );
  const caps = capabilities.data;
  const dataset = names.datasets.find((item) => item.id === datasetId);
  const readyDatasets = names.datasets.filter((item) => item.status === "ready");
  const cannotPatch = Boolean(caps) && !caps!.modes.human_guided_optimization;
  const autonomousAllowed = Boolean(workspace?.settings?.allow_autonomous_optimization);
  const holdoutSpent = Boolean(dataset) && runHoldout && dataset!.holdout_exposed && (dataset!.counts.holdout ?? 0) > 0;
  const refusal = start.error instanceof ApiError && start.error.status === 409 ? start.error : null;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const limits: Dict<number> = {};
    const put = (key: string, text: string) => {
      const value = parseOptionalNumber(text);
      if (value !== null) limits[key] = value;
    };
    put("max_model_calls", maxModelCalls);
    put("max_model_tokens", maxModelTokens);
    put("max_model_cost", maxModelCost);
    put("max_agent_calls", maxAgentCalls);
    const target = parseOptionalNumber(targetScore);
    // The API expresses scores and tolerances as fractions (0..1); the form uses percent and percentage points.
    const policy: Dict = {
      max_rounds: parseOptionalNumber(maxRounds),
      target_score: target === null ? null : target / 100,
      stagnation_rounds: Number(stagnation),
      min_improvement: Number(minImprovement) / 100,
      max_category_regression: Number(maxRegression) / 100,
      eval_mode: evalMode,
      run_holdout: runHoldout,
      limits,
    };
    void start.run(async () => {
      const result = await api.startOptimization({ agent_id: agentId, dataset_id: datasetId, mode, policy });
      navigate(`/optimizations/${result.optimization_run.id}`);
    });
  };

  return (
    <Card title="Start an optimization">
      <p className="muted">
        Kaizeval creates an isolated candidate copy of the agent's configuration, tries one allowlisted change per round, re-runs
        the whole regression set and keeps a change only if it passes every gate. The baseline is never modified and nothing is
        promoted to production automatically.
      </p>
      <ErrorNote error={names.error} />
      <form onSubmit={submit} className="form">
        <div className="form-grid">
          <Field label="Agent">
            <select value={agentId} onChange={(event) => { setAgentId(event.target.value); start.clear(); }} required>
              <option value="">Choose an agent</option>
              {names.agents.map((item) => (
                <option key={item.id} value={item.id}>{item.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Dataset">
            <select value={datasetId} onChange={(event) => { setDatasetId(event.target.value); start.clear(); }} required>
              <option value="">Choose a dataset</option>
              {readyDatasets.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name} ({item.counts.regression ?? 0} regression, {item.counts.holdout ?? 0} holdout)
                </option>
              ))}
            </select>
          </Field>
          <Field label="Mode"
            hint={mode === "autonomous"
              ? "Changes that pass every gate are kept without asking. Needs the owner role and the workspace setting that allows it."
              : "Every proposed change waits for a reviewer's approval before it is tried on the candidate."}>
            <select value={mode} onChange={(event) => { setMode(event.target.value); start.clear(); }}>
              <option value="human_guided">Human-guided</option>
              <option value="autonomous">Autonomous</option>
            </select>
          </Field>
          <Field label="Evaluation mode" hint={EVALUATION_MODES.find((item) => item.id === evalMode)?.explanation}>
            <select value={evalMode} onChange={(event) => setEvalMode(event.target.value)}>
              {EVALUATION_MODES.map((item) => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </select>
          </Field>
          <Field label="Max cycles (optional)" hint="One proposed change per cycle. Leave empty to run until the target is reached or the score stops changing.">
            <input type="number" value={maxRounds} min={1} max={200} step={1} onChange={(event) => setMaxRounds(event.target.value)} />
          </Field>
          <Field label="Target score (%) (optional)" hint="Stop as soon as the regression score reaches this value.">
            <input type="number" value={targetScore} min={0} max={100} step="any" onChange={(event) => setTargetScore(event.target.value)} />
          </Field>
          <Field label="Stop after unchanged cycles" hint="Stop when this many cycles in a row leave the score unchanged, then report the remaining recommendations.">
            <input type="number" value={stagnation} min={1} max={50} step={1} onChange={(event) => setStagnation(event.target.value)} required />
          </Field>
          <Field label="Minimum improvement (pp)" hint="A change is kept only if it beats the best version so far by at least this much.">
            <input type="number" value={minImprovement} min={0} max={100} step="any" onChange={(event) => setMinImprovement(event.target.value)} required />
          </Field>
          <Field label="Max category regression (pp)" hint="A change is rolled back if any category drops by more than this against the baseline.">
            <input type="number" value={maxRegression} min={0} max={100} step="any" onChange={(event) => setMaxRegression(event.target.value)} required />
          </Field>
        </div>
        <Check label="Validate the final candidate on the sealed holdout" checked={runHoldout} onChange={(value) => { setRunHoldout(value); start.clear(); }}
          hint="An independent check on cases the optimizer never saw. Only aggregate results are reported." />
        <fieldset className="fieldset">
          <legend>Budgets (optional)</legend>
          <div className="form-grid">
            <Field label="Max model calls">
              <input type="number" value={maxModelCalls} min={1} step={1} onChange={(event) => setMaxModelCalls(event.target.value)} />
            </Field>
            <Field label="Max model tokens">
              <input type="number" value={maxModelTokens} min={1} step={1} onChange={(event) => setMaxModelTokens(event.target.value)} />
            </Field>
            <Field label="Max estimated model cost" hint="Only effective when per-token prices are set on the provider.">
              <input type="number" value={maxModelCost} min={0} step="any" onChange={(event) => setMaxModelCost(event.target.value)} />
            </Field>
            <Field label="Max agent calls">
              <input type="number" value={maxAgentCalls} min={1} step={1} onChange={(event) => setMaxAgentCalls(event.target.value)} />
            </Field>
          </div>
        </fieldset>

        <ErrorNote error={capabilities.error} />
        {cannotPatch && caps && <CapabilityBlock capabilities={caps} />}
        {!cannotPatch && mode === "autonomous" && !autonomousAllowed && (
          <Notice tone="warn" title="Autonomous optimization is disabled for this workspace">
            An owner can enable it under <Link to="/workspaces">Workspaces and members</Link>, or use human-guided mode.
          </Notice>
        )}
        {!cannotPatch && mode === "autonomous" && !can("owner") && (
          <Notice tone="warn">Starting an autonomous optimization needs the owner role.</Notice>
        )}
        {holdoutSpent && dataset && (
          <Notice tone="warn" title="The holdout of this dataset has been exposed">
            It already informed a decision or was opened by a person. <Link to={`/datasets/${dataset.id}`}>Rotate the holdout</Link>{" "}
            first, or switch off holdout validation above.
          </Notice>
        )}
        {refusal ? (
          <Blocked code={refusal.code} message={refusal.message} />
        ) : (
          <ErrorNote error={start.error} />
        )}
        <div className="button-row">
          <button type="submit" className="btn btn-primary" disabled={start.busy || !agentId || !datasetId || cannotPatch}>
            {start.busy ? "Starting…" : "Start optimization"}
          </button>
        </div>
      </form>
    </Card>
  );
}

export function OptimizationsPage() {
  const { api, can } = useSession();
  const names = useNames();
  const [offset, setOffset] = useState(0);
  const runs = useLoad(() => api.optimizations({ limit: PAGE, offset }), [api, offset]);
  const candidates = useLoad(() => api.candidates(), [api]);
  const data = runs.data;

  return (
    <>
      <PageHeader title="Optimization" subtitle="Bounded, gated improvement of an isolated candidate against an untouched baseline." />
      {can("editor") ? <StartForm /> : <p className="muted small">Only editors and owners can start an optimization.</p>}

      <Card title="Optimization runs" actions={<button type="button" className="btn btn-small" onClick={runs.reload}>Refresh</button>}>
        {runs.loading && !data && <Loading />}
        <ErrorNote error={runs.error} />
        {data && data.items.length === 0 && (
          <Empty title="No optimization run yet.">
            {can("editor") ? "Start one above after a baseline evaluation." : "An editor needs to start an optimization."}
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
                  <th scope="col">Mode</th>
                  <th scope="col">Status</th>
                  <th scope="col" className="num">Rounds</th>
                  <th scope="col" className="num">Baseline</th>
                  <th scope="col" className="num">Best</th>
                  <th scope="col">Holdout check</th>
                  <th scope="col">Stop reason</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((run) => (
                  <tr key={run.id}>
                    <th scope="row">
                      <Link to={`/optimizations/${run.id}`}><When value={run.created_at} /></Link> {run.synthetic && <SyntheticBadge />}
                    </th>
                    <td>{names.agentName(run.agent_id)}</td>
                    <td>{names.datasetName(run.dataset_id)}</td>
                    <td>{humanize(run.mode)}</td>
                    <td><StatusBadge status={run.status} /></td>
                    <td className="num">{run.state.round ?? 0}</td>
                    <td className="num">{formatPercent(run.state.baseline_overall)}</td>
                    <td className="num">{formatPercent(run.state.best_overall)}</td>
                    <td>
                      {run.holdout?.status === "completed"
                        ? `${formatPp(run.holdout.overall?.delta_percentage_points)} (${run.holdout.overall?.verdict})`
                        : humanize(run.holdout?.status ?? "not run")}
                    </td>
                    <td>{run.stop_reason ?? run.error ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
        {data && <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />}
      </Card>

      <Card title="Candidates">
        <p className="muted">Each optimization works on its own candidate copy. A candidate reaches production only through an explicit release decision.</p>
        {candidates.loading && !candidates.data && <Loading />}
        <ErrorNote error={candidates.error} />
        {candidates.data && candidates.data.items.length === 0 && <p className="muted">No candidate exists yet.</p>}
        {candidates.data && candidates.data.items.length > 0 && (
          <TableWrap>
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Candidate</th>
                  <th scope="col">Agent</th>
                  <th scope="col">Status</th>
                  <th scope="col">Created</th>
                </tr>
              </thead>
              <tbody>
                {candidates.data.items.map((candidate) => (
                  <tr key={candidate.id}>
                    <th scope="row"><Link to={`/candidates/${candidate.id}`}><code>{candidate.external_id}</code></Link></th>
                    <td>{names.agentName(candidate.agent_id)}</td>
                    <td><StatusBadge status={candidate.status} /></td>
                    <td><When value={candidate.created_at} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Card>
    </>
  );
}
