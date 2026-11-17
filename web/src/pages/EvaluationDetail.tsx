import { Fragment, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useSession } from "../auth";
import { EvidenceRefs } from "../components/Evidence";
import { isActive, JobProgress } from "../components/JobProgress";
import { ReportButtons } from "../components/ReportButtons";
import { UsageBoxes } from "../components/UsageBoxes";
import {
  Badge, Card, ConfirmDialog, CountList, Empty, ErrorNote, Field, Loading, Notice, PageHeader, Pager, ScoreCell, Stat,
  StatusBadge, SyntheticBadge, SyntheticBanner, TableWrap, Tabs, When,
} from "../components/ui";
import { formatCost, formatMs, formatPercent, humanize } from "../format";
import { useAction, useLoad, useNames } from "../hooks";
import type { CaseResult, EvaluationRun, RunSummary } from "../types";

const PAGE = 25;
const VERDICTS = ["pass", "fail", "needs_human_review", "n/a"];

function methodCounts(methods: Record<string, number>): string {
  return Object.entries(methods).map(([method, count]) => `${humanize(method).toLowerCase()}: ${count}`).join(", ");
}

function Summary({ run, summary }: { run: EvaluationRun; summary: RunSummary }) {
  const agreement = summary.evaluator_agreement;
  return (
    <>
      <Card title="Result">
        {summary.partial && (
          <Notice tone="warn" title="Partial result">
            The run stopped before every case was evaluated. The figures below cover the cases that were completed.
          </Notice>
        )}
        <div className="stat-row">
          <Stat label="Overall score" value={formatPercent(summary.overall)} note={run.synthetic ? "synthetic, not real quality" : undefined} />
          <Stat label="Pass rate" value={formatPercent(summary.pass_rate)} note={`${summary.scored} of ${summary.cases} cases scored`} />
          <Stat label="Critical failures" value={summary.critical_failures} />
          <Stat label="Needs human review" value={summary.needs_human_review} />
          <Stat label="Agent errors" value={summary.agent_errors} />
          <Stat label="Latency p50 / p95" value={`${formatMs(summary.latency_ms.p50)} / ${formatMs(summary.latency_ms.p95)}`} />
        </div>
        <h3>Caveats</h3>
        <ul className="caveats">
          {summary.caveats.map((caveat, index) => <li key={index}>{caveat}</li>)}
        </ul>
      </Card>

      <Card title="Scores by dimension">
        <TableWrap>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Dimension</th>
                <th scope="col">Mean score</th>
                <th scope="col" className="num">Cases</th>
                <th scope="col">Scoring method (cases)</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(summary.by_dimension).map(([dimension, value]) => (
                <tr key={dimension}>
                  <th scope="row">{humanize(dimension)}</th>
                  <td><ScoreCell value={value.mean} /></td>
                  <td className="num">{value.n}</td>
                  <td>
                    {methodCounts(value.methods)}{" "}
                    {value.methods.model_judge ? <Badge tone="info" title="Scored by a model judge: an estimate, not a verified fact.">model-estimated</Badge> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
        {Object.keys(summary.by_dimension).length === 0 && <p className="muted">No dimension was scored.</p>}
      </Card>

      <Card title="Scores by category">
        <TableWrap>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Category</th>
                <th scope="col" className="num">Cases</th>
                <th scope="col" className="num">Scored</th>
                <th scope="col">Mean score</th>
                <th scope="col" className="num">Pass rate</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(summary.by_category).map(([category, value]) => (
                <tr key={category}>
                  <th scope="row">{humanize(category)}</th>
                  <td className="num">{value.n}</td>
                  <td className="num">{value.scored}</td>
                  <td><ScoreCell value={value.mean} /></td>
                  <td className="num">{formatPercent(value.pass_rate)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      </Card>

      <div className="two-col">
        <Card title="Evaluator agreement">
          {agreement ? (
            <>
              <div className="stat-row">
                <Stat label="Agreement rate" value={formatPercent(agreement.rate)} note={`${agreement.agreed} of ${agreement.compared} compared cases`} />
                <Stat label="Sent to the arbiter" value={agreement.arbitrated} />
              </div>
              <p className="muted small">Agreement between two judges is not ground truth; judges can share the same bias.</p>
            </>
          ) : (
            <p className="muted">Only reported for dual-judge runs. This run used {run.mode} mode.</p>
          )}
        </Card>
        <Card title="Reference basis">
          <p className="muted small">How many cases were scored against each kind of reference answer.</p>
          <CountList counts={summary.reference_basis} />
        </Card>
      </div>

      <Card title="Usage">
        <UsageBoxes models={summary.model_usage} agent={summary.agent} />
      </Card>
    </>
  );
}

function Conversation({ result }: { result: CaseResult }) {
  const turns = result.case?.turns ?? [];
  const replies = result.response.turns ?? [];
  const count = Math.max(turns.length, replies.length);
  const items = [];
  for (let index = 0; index < count; index += 1) {
    const turn = turns[index];
    const reply = replies[index];
    if (turn) {
      items.push(
        <li key={`q${index}`} className="turn turn-user">
          <span className="turn-role">User</span>
          <p>{turn.content}</p>
        </li>,
      );
    }
    if (reply) {
      items.push(
        <li key={`a${index}`} className="turn turn-agent">
          <span className="turn-role">Agent · {formatMs(reply.latency_ms)}</span>
          <p>{reply.text || <span className="muted">(empty answer)</span>}</p>
          <p className="muted small">
            Citations: {reply.citations && reply.citations.length > 0 ? reply.citations.join("; ") : "none"}
          </p>
        </li>,
      );
    }
  }
  if (items.length === 0) return <p className="muted">The conversation is not available for this result.</p>;
  return <ol className="conversation">{items}</ol>;
}

function ResultDetail({ result }: { result: CaseResult }) {
  const final = result.final ?? { overall: null, verdict: "n/a" };
  const expected = result.case?.expected;
  const scores = Object.entries(final.scores ?? {});
  return (
    <div className="result-detail">
      {result.error && (
        <div className="alert alert-error" role="alert">
          <p>The agent call failed: {result.error}</p>
        </div>
      )}
      <div className="two-col">
        <section>
          <h4>Conversation</h4>
          <Conversation result={result} />
        </section>
        <section>
          <h4>Expected</h4>
          {expected ? (
            expected.behavior === "abstain" ? (
              <p><Badge tone="info">Should decline</Badge> The knowledge base does not cover this question.</p>
            ) : (
              <>
                <p>{expected.answer || <span className="muted">no reference answer</span>}</p>
                <p className="muted small">Key facts: {expected.key_facts?.length ? expected.key_facts.join("; ") : "none"}</p>
                {expected.follow_up_answer && <p>Follow-up: {expected.follow_up_answer}</p>}
                {expected.follow_up_key_facts && expected.follow_up_key_facts.length > 0 && (
                  <p className="muted small">Follow-up key facts: {expected.follow_up_key_facts.join("; ")}</p>
                )}
              </>
            )
          ) : (
            <p className="muted">The test case is no longer available.</p>
          )}
          {result.case && (
            <p className="muted small">
              Reference status: {humanize(result.case.answer_status)}
              {result.case.stale ? " · stale evidence (document version replaced)" : ""}
            </p>
          )}
          <h4>Evidence</h4>
          <EvidenceRefs refs={result.case?.evidence_refs} />
        </section>
      </div>

      <h4>Scores by dimension</h4>
      {scores.length === 0 ? (
        <p className="muted">{final.reason ?? "No dimension could be scored."}</p>
      ) : (
        <TableWrap>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Dimension</th>
                <th scope="col">Score</th>
                <th scope="col">Method</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {scores.map(([dimension, score]) => (
                <tr key={dimension}>
                  <th scope="row">{humanize(dimension)}</th>
                  <td><ScoreCell value={score.score} /></td>
                  <td>
                    <Badge tone={score.method === "deterministic" ? "ok" : score.method === "model_judge" ? "info" : "bad"}>
                      {humanize(score.method)}
                    </Badge>
                  </td>
                  <td>
                    {score.detail || (score.method === "model_judge" ? "Estimated by a model judge." : "—")}
                    {result.deterministic[dimension]?.missing && result.deterministic[dimension].missing!.length > 0 && (
                      <span className="muted small"> Missing: {result.deterministic[dimension].missing!.join("; ")}</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
      {final.reason && scores.length > 0 && <p className="muted">{final.reason}</p>}

      <div className="flag-row">
        {final.critical_failure && <Badge tone="bad">Critical failure</Badge>}
        {final.needs_human_review && <Badge tone="warn">Needs human review</Badge>}
        {final.model_estimated && <Badge tone="info">Includes model-estimated scores</Badge>}
        {final.conflicts && final.conflicts.length > 0 && (
          <Badge tone="warn" title="A deterministic check and a model judge disagreed strongly on these dimensions.">
            Conflicts: {final.conflicts.map(humanize).join(", ")}
          </Badge>
        )}
        {final.judge?.agreement === true && <Badge tone="ok">Judges agreed</Badge>}
        {final.judge?.agreement === false && <Badge tone="warn">Judges disagreed</Badge>}
        {final.judge?.arbitrated && <Badge tone="info">Decided by the arbiter</Badge>}
      </div>
      {final.judge_error && <p className="error-text">Judge error: {final.judge_error}</p>}
      {final.attempt_overalls && (
        <p className="muted small">Overall score of each repeat: {final.attempt_overalls.map((value) => formatPercent(value)).join(", ")}</p>
      )}

      <h4>Recorded judgments</h4>
      {result.judgments.length === 0 ? (
        <p className="muted">No model judgment was recorded for this case.</p>
      ) : (
        <TableWrap>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Role</th>
                <th scope="col">Model</th>
                <th scope="col">Scores</th>
                <th scope="col">Verdict</th>
                <th scope="col" className="num">Confidence</th>
                <th scope="col">Rationale</th>
              </tr>
            </thead>
            <tbody>
              {result.judgments.map((judgment, index) => (
                <tr key={index}>
                  <th scope="row">{humanize(judgment.role)}</th>
                  <td>
                    {judgment.model} {judgment.synthetic && <SyntheticBadge />}
                    <span className="muted small"> · {judgment.prompt_version}</span>
                  </td>
                  <td>
                    {Object.entries(judgment.scores ?? {}).map(([dimension, value]) => (
                      <span key={dimension} className="score-pair">{humanize(dimension)} {formatPercent(value)}</span>
                    ))}
                  </td>
                  <td><StatusBadge status={judgment.verdict} /></td>
                  <td className="num">{formatPercent(judgment.confidence)}</td>
                  <td className="cell-wide">{judgment.rationale}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
      <p className="muted small">
        Latency {formatMs(result.latency_ms)} · agent cost {formatCost(result.agent_cost)}
      </p>
    </div>
  );
}

function ResultsTable({ runId }: { runId: string }) {
  const { api } = useSession();
  const [verdict, setVerdict] = useState("");
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState<string | null>(null);
  const results = useLoad(() => api.runResults(runId, { verdict: verdict || undefined, limit: PAGE, offset }), [api, runId, verdict, offset]);
  const data = results.data;

  return (
    <>
      <div className="filters">
        <Field label="Filter by verdict">
          <select value={verdict} onChange={(event) => { setVerdict(event.target.value); setOffset(0); setExpanded(null); }}>
            <option value="">All verdicts</option>
            {VERDICTS.map((item) => (
              <option key={item} value={item}>{item === "n/a" ? "Not scored (n/a)" : humanize(item)}</option>
            ))}
          </select>
        </Field>
      </div>
      {results.loading && !data && <Loading />}
      <ErrorNote error={results.error} />
      {data && data.items.length === 0 && <Empty title={verdict ? "No result has this verdict." : "This run has no results yet."} />}
      {data && data.items.length > 0 && (
        <TableWrap>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Question</th>
                <th scope="col">Category</th>
                <th scope="col">Severity</th>
                <th scope="col">Verdict</th>
                <th scope="col">Overall</th>
                <th scope="col">Flags</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((result) => {
                const open = expanded === result.id;
                return (
                  <Fragment key={result.id}>
                    <tr className={open ? "row-open" : undefined}>
                      <td className="cell-wide">
                        <button type="button" className="expander" aria-expanded={open}
                          onClick={() => setExpanded(open ? null : result.id)}>
                          <span className="expander-icon" aria-hidden="true">{open ? "−" : "+"}</span>
                          <span>{result.case?.turns?.[0]?.content ?? `Case ${result.case_id.slice(0, 8)}`}</span>
                        </button>
                      </td>
                      <td>{humanize(result.case?.category)}</td>
                      <td><StatusBadge status={result.case?.severity} /></td>
                      <td><StatusBadge status={result.verdict} /></td>
                      <td><ScoreCell value={result.overall} /></td>
                      <td>
                        <span className="flag-row">
                          {result.final?.critical_failure && <Badge tone="bad">Critical</Badge>}
                          {result.final?.needs_human_review && <Badge tone="warn">Review</Badge>}
                          {result.error && <Badge tone="bad">Agent error</Badge>}
                        </span>
                      </td>
                    </tr>
                    {open && (
                      <tr className="row-detail">
                        <td colSpan={6}>
                          <ResultDetail result={result} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </TableWrap>
      )}
      {data && <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={(next) => { setOffset(next); setExpanded(null); }} />}
    </>
  );
}

function Results({ run }: { run: EvaluationRun }) {
  const { can } = useSession();
  const [unsealed, setUnsealed] = useState(false);
  const [confirming, setConfirming] = useState(false);

  if (run.split !== "holdout" || unsealed) {
    return (
      <Card title="Results">
        {run.split === "holdout" && (
          <Notice tone="warn" title="Holdout results are now exposed">
            This access was audited and the holdout is marked as exposed. Rotate it before it validates further tuning.
          </Notice>
        )}
        <ResultsTable runId={run.id} />
      </Card>
    );
  }
  return (
    <Card title="Results">
      <Notice title="Per-case results of a holdout run are sealed">
        <p>
          This run used the sealed holdout, so only aggregate figures are shown. Opening the per-case results is recorded in the
          audit log, marks the holdout as exposed and forces a rotation before it can validate another optimization.
        </p>
      </Notice>
      {can("reviewer") ? (
        <button type="button" className="btn btn-danger" onClick={() => setConfirming(true)}>
          Open holdout results
        </button>
      ) : (
        <p className="muted small">Opening holdout results needs the reviewer role or higher.</p>
      )}
      <ConfirmDialog open={confirming} title="Open sealed holdout results?" confirmLabel="Open holdout results" danger
        onCancel={() => setConfirming(false)} onConfirm={() => { setConfirming(false); setUnsealed(true); }}>
        <p>You are about to read the per-case results of a holdout run.</p>
        <ul>
          <li>This access is written to the audit log with your account.</li>
          <li>The holdout is marked as exposed and can no longer serve as an independent check.</li>
          <li>It must be rotated before the next optimization can use holdout validation.</li>
        </ul>
      </ConfirmDialog>
    </Card>
  );
}

function Findings({ runId }: { runId: string }) {
  const { api } = useSession();
  const [offset, setOffset] = useState(0);
  const findings = useLoad(() => api.runFindings(runId, { limit: 50, offset }), [api, runId, offset]);
  const data = findings.data;
  return (
    <Card title={`Findings${data ? ` (${data.total})` : ""}`}>
      <p className="muted">
        An observation is what was measured in the answer. A hypothesis is a suspected cause that cannot be seen from outside the
        agent; treat it as a lead, not a fact.
      </p>
      {findings.loading && !data && <Loading />}
      <ErrorNote error={findings.error} />
      {data && data.items.length === 0 && (
        <Empty title="No findings for this run.">
          Findings are produced for completed regression runs. Holdout runs never produce per-case feedback.
        </Empty>
      )}
      {data && data.items.length > 0 && (
        <TableWrap>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Category</th>
                <th scope="col">Severity</th>
                <th scope="col">Question</th>
                <th scope="col">Observation (measured)</th>
                <th scope="col">Hypothesis (suspected cause)</th>
                <th scope="col" className="num">Confidence</th>
                <th scope="col">Source</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((finding) => (
                <tr key={finding.id}>
                  <th scope="row">{humanize(finding.category)}</th>
                  <td><StatusBadge status={finding.severity} /></td>
                  <td className="cell-wide">{typeof finding.evidence.question === "string" ? finding.evidence.question : "—"}</td>
                  <td className="cell-wide">{finding.observation}</td>
                  <td className="cell-wide"><em>{finding.hypothesis || "—"}</em></td>
                  <td className="num">{formatPercent(finding.confidence)}</td>
                  <td>{finding.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
      {data && <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />}
    </Card>
  );
}

export function EvaluationDetailPage() {
  const { id = "" } = useParams();
  const { api, can, userLabel } = useSession();
  const names = useNames();
  const run = useLoad(() => api.run(id), [api, id]);
  const [tab, setTab] = useState<"summary" | "results" | "findings">("summary");
  const cancel = useAction();
  const data = run.data;
  const active = data ? isActive(data.status) : false;

  return (
    <>
      <PageHeader title="Evaluation run" subtitle={<Link to="/evaluations">All evaluations</Link>}
        actions={
          <>
            <button type="button" className="btn btn-small" onClick={run.reload}>Refresh</button>
            {data && data.status === "completed" && data.split === "regression" && (
              <Link className="btn btn-small" to={`/recommendations?run_id=${data.id}`}>Recommendations</Link>
            )}
            {data && <ReportButtons objectId={data.id} kind="evaluation" />}
          </>
        } />
      {run.loading && !data && <Loading />}
      <ErrorNote error={run.error} />
      {data && (
        <>
          <SyntheticBanner show={data.synthetic} detail="Judge scores are text-overlap heuristics, not a semantic evaluation." />
          <Card title="Run">
            <dl className="kv">
              <dt>Status</dt>
              <dd><StatusBadge status={data.status} /></dd>
              <dt>Agent</dt>
              <dd><Link to={`/agents/${data.agent_id}`}>{names.agentName(data.agent_id)}</Link>, version <code>{data.agent_version}</code></dd>
              <dt>Dataset</dt>
              <dd>
                <Link to={`/datasets/${data.dataset_id}`}>{names.datasetName(data.dataset_id)}</Link>, version {data.dataset_version},{" "}
                {data.split} set
              </dd>
              <dt>Mode</dt>
              <dd>{data.mode} ({humanize(data.kind)} run)</dd>
              <dt>Started</dt>
              <dd><When value={data.created_at} /> by {userLabel(data.created_by)}</dd>
              <dt>Finished</dt>
              <dd><When value={data.finished_at} /></dd>
              {data.optimization_run_id && (
                <>
                  <dt>Part of</dt>
                  <dd><Link to={`/optimizations/${data.optimization_run_id}`}>an optimization run</Link></dd>
                </>
              )}
              {data.agent_config_digest && (
                <>
                  <dt>Agent configuration digest</dt>
                  <dd><code className="wrap">{data.agent_config_digest}</code></dd>
                </>
              )}
            </dl>
            {data.error && (
              <div className="alert alert-error" role="alert">
                <p>{data.error}</p>
              </div>
            )}
            {data.job_id && active && (
              <JobProgress key={data.job_id} jobId={data.job_id} label="Evaluation" onSettled={run.reload}>
                {can("editor") && (
                  <button type="button" className="btn btn-small btn-danger" disabled={cancel.busy}
                    onClick={() => void cancel.run(async () => { await api.cancelRun(data.id); run.reload(); })}>
                    {cancel.busy ? "Cancelling…" : "Cancel"}
                  </button>
                )}
              </JobProgress>
            )}
            {!data.job_id && active && (
              <p className="muted">This run is still in progress as part of an optimization. Use Refresh to update it.</p>
            )}
            <ErrorNote error={cancel.error} />
          </Card>

          <Tabs current={tab} onChange={setTab}
            tabs={[{ id: "summary", label: "Summary" }, { id: "results", label: "Results" }, { id: "findings", label: "Findings" }]} />

          {tab === "summary" && (
            data.summary ? <Summary run={data} summary={data.summary} /> : (
              <Card>
                <Empty title="No summary yet.">
                  {active ? "The summary appears when the run finishes." : "This run did not produce a summary."}
                </Empty>
              </Card>
            )
          )}
          {tab === "results" && <Results run={data} />}
          {tab === "findings" && <Findings runId={data.id} />}
        </>
      )}
    </>
  );
}
