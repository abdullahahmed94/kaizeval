import { useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useSession } from "../auth";
import { GroupDeltaTable, PairedSummary, SafetyStats } from "../components/Comparison";
import { isActive, JobProgress } from "../components/JobProgress";
import { ReportButtons } from "../components/ReportButtons";
import { UsageBoxes } from "../components/UsageBoxes";
import {
  Badge, Card, ConfirmDialog, DiffBlock, Empty, ErrorNote, Field, JsonBlock, Loading, Notice, PageHeader, Stat, StatusBadge,
  SyntheticBadge, SyntheticBanner, When,
} from "../components/ui";
import { formatPercent, formatPp, humanize } from "../format";
import { useAction, useLoad, useNames } from "../hooks";
import type { HoldoutResult, Job, OptimizationRun, Patch } from "../types";

const OUTCOME_LABELS: Record<string, string> = {
  kept: "Kept", rolled_back: "Rolled back", rejected: "Rejected by reviewer", invalid: "Invalid proposal",
  proposed: "Waiting for approval", approved: "Approved, not yet evaluated", applied: "Applied, being evaluated",
};

interface Round {
  round: number;
  patch: Record<string, unknown>;
  outcome: string;
  overall?: number;
  deltaPp?: number;
  reasons: string[];
  evaluationRunId?: string;
  detail?: Patch;
}

/** One entry per round: the recorded history, completed with the patch row (diff, rationale, risk). */
function buildRounds(run: OptimizationRun): Round[] {
  const patches = new Map((run.patches ?? []).map((patch) => [patch.round, patch]));
  const rounds: Round[] = (run.state.history ?? []).map((entry) => ({
    round: entry.round, patch: entry.patch, outcome: entry.outcome, overall: entry.overall,
    deltaPp: entry.delta_vs_baseline_pp, reasons: entry.reasons ?? [], evaluationRunId: entry.evaluation_run_id,
    detail: patches.get(entry.round),
  }));
  const recorded = new Set(rounds.map((item) => item.round));
  for (const patch of run.patches ?? []) {
    if (!recorded.has(patch.round)) {
      rounds.push({
        round: patch.round, patch: patch.patch, outcome: patch.state, overall: patch.outcome?.overall,
        deltaPp: patch.outcome?.delta_vs_baseline_pp, reasons: patch.outcome?.reasons ?? [],
        evaluationRunId: patch.outcome?.evaluation_run_id, detail: patch,
      });
    }
  }
  return rounds.sort((a, b) => a.round - b.round);
}

function Timeline({ run }: { run: OptimizationRun }) {
  const { userLabel } = useSession();
  const rounds = buildRounds(run);
  if (rounds.length === 0) {
    return <Empty title="No round has been recorded yet.">Rounds appear here as the optimizer proposes changes.</Empty>;
  }
  return (
    <ol className="timeline">
      {rounds.map((item) => (
        <li key={item.round} className={`timeline-item outcome-${item.outcome}`}>
          <div className="item-head">
            <h3>
              Round {item.round} {item.detail?.synthetic && <SyntheticBadge />}
            </h3>
            <StatusBadge status={item.outcome} />
          </div>
          {OUTCOME_LABELS[item.outcome] && OUTCOME_LABELS[item.outcome] !== humanize(item.outcome) && (
            <p className="muted small">{OUTCOME_LABELS[item.outcome]}</p>
          )}
          <div className="two-col">
            <div>
              <p className="muted small">Patch</p>
              <JsonBlock value={item.patch} />
            </div>
            <div>
              <p className="muted small">Diff against the candidate before this round</p>
              <DiffBlock diff={item.detail?.diff ?? null} />
            </div>
          </div>
          {item.detail && (
            <>
              <p>
                <span className="muted small">Rationale: </span>
                {item.detail.rationale || "—"}
              </p>
              <p className="muted small">
                Risk: <StatusBadge status={item.detail.risk} />
                {item.detail.decided_by ? ` · decided by ${userLabel(item.detail.decided_by)}` : ""}
                {item.detail.decision_note ? ` · ${item.detail.decision_note}` : ""}
              </p>
            </>
          )}
          {(item.overall !== undefined || item.deltaPp !== undefined) && (
            <div className="stat-row">
              <Stat label="Regression score" value={formatPercent(item.overall)} />
              <Stat label="Change against the baseline" value={formatPp(item.deltaPp)} />
              {item.detail?.outcome?.verdict && <Stat label="Paired verdict" value={<StatusBadge status={item.detail.outcome.verdict} />} />}
            </div>
          )}
          {item.reasons.length > 0 && (
            <>
              <p className="muted small">{item.outcome === "kept" ? "Notes" : "Gate reasons"}</p>
              <ul>
                {item.reasons.map((reason, index) => <li key={index}>{reason}</li>)}
              </ul>
            </>
          )}
          {item.evaluationRunId && (
            <p className="small">
              <Link to={`/evaluations/${item.evaluationRunId}`}>Evaluation run of this round</Link>
            </p>
          )}
        </li>
      ))}
    </ol>
  );
}

function PendingChange({ patch, onDecided }: { patch: Patch; onDecided: () => void }) {
  const { api, can } = useSession();
  const [note, setNote] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const action = useAction();

  const decide = (decision: "approved" | "rejected") => {
    setLocalError(null);
    if (decision === "rejected" && !note.trim()) {
      setLocalError("Give a reason before rejecting this change.");
      return;
    }
    void action.run(async () => {
      await api.decidePatch(patch.id, decision, note.trim() || null);
      onDecided();
    });
  };

  return (
    <section className="card pending" aria-labelledby="pending-title">
      <h2 id="pending-title">Pending change, round {patch.round} {patch.synthetic && <SyntheticBadge />}</h2>
      <p>
        The optimizer proposes the change below. It is applied only to the isolated candidate, then the whole regression set is
        re-run. Production and the baseline are not touched.
      </p>
      <DiffBlock diff={patch.diff} />
      <p>
        <span className="muted small">Rationale: </span>
        {patch.rationale || "—"}
      </p>
      <p className="muted small">Risk: <StatusBadge status={patch.risk} /></p>
      {can("reviewer") ? (
        <div className="form">
          <Field label="Note (a reason is required to reject)">
            <input type="text" value={note} onChange={(event) => setNote(event.target.value)} maxLength={2000} />
          </Field>
          <div className="button-row">
            <button type="button" className="btn btn-primary" disabled={action.busy} onClick={() => decide("approved")}>
              Approve change
            </button>
            <button type="button" className="btn btn-danger" disabled={action.busy} onClick={() => decide("rejected")}>
              Reject change
            </button>
          </div>
          {localError && <p className="error-text" role="alert">{localError}</p>}
          <ErrorNote error={action.error} />
        </div>
      ) : (
        <p className="muted small">Approving or rejecting a change needs the reviewer role or higher.</p>
      )}
    </section>
  );
}

function HoldoutPanel({ holdout, finished }: { holdout: HoldoutResult | null; finished: boolean }) {
  return (
    <Card title="Independent holdout validation">
      <p className="muted">
        The baseline and the final candidate are both run on sealed cases that the optimizer never saw. Only aggregates are
        reported; no case text or per-case score leaves the holdout.
      </p>
      {!holdout && (
        <p className="muted">
          {finished
            ? "Holdout validation was not run: this optimization ended before its final validation step."
            : "Not run yet. It runs once, after the last round, when at least one change was kept."}
        </p>
      )}
      {holdout && holdout.status !== "completed" && (
        <Notice tone={holdout.status === "failed" ? "warn" : "info"} title={`Holdout validation ${holdout.status}`}>
          <p>{holdout.reason ?? "No reason was recorded."}</p>
        </Notice>
      )}
      {holdout && holdout.status === "completed" && holdout.overall && (
        <>
          {holdout.synthetic && <p><SyntheticBadge /> <span className="muted small">Not a real evaluation.</span></p>}
          <PairedSummary overall={holdout.overall} />
          <p className="muted small">
            Holdout generation {holdout.generation}, {holdout.paired_cases} paired sealed case(s).
          </p>
          <GroupDeltaTable caption="By category (aggregate only)" groups={holdout.by_category} />
          <SafetyStats critical={holdout.critical_failures} latency={holdout.latency_ms} />
          {holdout.note && <Notice>{holdout.note}</Notice>}
        </>
      )}
    </Card>
  );
}

export function OptimizationDetailPage() {
  const { id = "" } = useParams();
  const { api, can, userLabel } = useSession();
  const names = useNames();
  const run = useLoad(() => api.optimization(id), [api, id]);
  const report = useLoad(() => api.optimizationReport(id), [api, id]);
  const stop = useAction();
  const [confirmStop, setConfirmStop] = useState(false);
  const [watch, setWatch] = useState(0);
  const lastProgressRun = useRef<string | undefined>(undefined);
  const data = run.data;
  const comparison = report.data?.regression_comparison ?? null;

  const reload = () => {
    run.reload();
    report.reload();
  };
  // Restart the job watcher too: after a decision the paused job is queued again.
  const reloadAndWatch = () => {
    reload();
    setWatch((value) => value + 1);
  };

  // Each evaluation inside the job reports its own run id; a new id means a round has moved on.
  const tick = (job: Job) => {
    const current = job.progress?.run_id;
    if (current !== lastProgressRun.current) {
      lastProgressRun.current = current;
      reload();
    }
  };

  const stoppable = data ? ["queued", "running", "review_required"].includes(data.status) : false;
  const pending = data && data.status === "review_required"
    ? (data.patches ?? []).find((patch) => patch.id === data.state.pending_patch_id && patch.state === "proposed")
    : undefined;
  const policy = data?.policy;

  return (
    <>
      <PageHeader title="Optimization run" subtitle={<Link to="/optimizations">All optimization runs</Link>}
        actions={
          <>
            <button type="button" className="btn btn-small" onClick={reloadAndWatch}>Refresh</button>
            {data && <ReportButtons objectId={data.id} kind="optimization" />}
            {data && stoppable && can("editor") && (
              <button type="button" className="btn btn-danger" disabled={stop.busy} onClick={() => setConfirmStop(true)}>
                Stop
              </button>
            )}
          </>
        } />
      {run.loading && !data && <Loading />}
      <ErrorNote error={run.error} />
      <ErrorNote error={stop.error} />
      {data && (
        <>
          <SyntheticBanner show={data.synthetic}
            detail="It demonstrates the workflow only and is not evidence of real agent quality or improvement." />
          <Card title="Run">
            <dl className="kv">
              <dt>Status</dt>
              <dd><StatusBadge status={data.status} /></dd>
              <dt>Stop reason</dt>
              <dd>{data.stop_reason ?? (stoppable ? "still in progress" : "—")}</dd>
              <dt>Mode</dt>
              <dd>{humanize(data.mode)}</dd>
              <dt>Agent</dt>
              <dd><Link to={`/agents/${data.agent_id}`}>{names.agentName(data.agent_id)}</Link></dd>
              <dt>Dataset</dt>
              <dd><Link to={`/datasets/${data.dataset_id}`}>{names.datasetName(data.dataset_id)}</Link></dd>
              <dt>Started</dt>
              <dd><When value={data.created_at} /> by {userLabel(data.created_by)}</dd>
              <dt>Finished</dt>
              <dd><When value={data.finished_at} /></dd>
              <dt>Candidate</dt>
              <dd>
                {data.candidate_id ? <Link to={`/candidates/${data.candidate_id}`}>Open Candidate B and its release decision</Link>
                  : "not created yet"}
              </dd>
            </dl>
            {data.error && (
              <div className="alert alert-error" role="alert">
                <p>{data.error}</p>
              </div>
            )}
            {data.job_id && stoppable && (
              <JobProgress key={`${data.job_id}:${watch}`} jobId={data.job_id} label="Current evaluation" onSettled={reload} onTick={tick} />
            )}
            <div className="stat-row">
              <Stat label="Baseline A (regression)" value={formatPercent(data.state.baseline_overall)}
                note={data.baseline_run_id ? <Link to={`/evaluations/${data.baseline_run_id}`}>baseline run</Link> : undefined} />
              <Stat label="Best version (regression)" value={formatPercent(data.state.best_overall)}
                note={data.best_run_id && data.best_run_id !== data.baseline_run_id
                  ? <Link to={`/evaluations/${data.best_run_id}`}>best run</Link> : undefined} />
              <Stat label="Rounds" value={`${data.state.round ?? 0} of ${policy?.max_rounds ?? "?"}`} />
              <Stat label="Rounds without a kept change" value={`${data.state.stagnation ?? 0} of ${policy?.stagnation_rounds ?? "?"}`} />
            </div>
            {policy && (
              <details>
                <summary>Policy of this run</summary>
                <dl className="kv">
                  <dt>Target score</dt>
                  <dd>{policy.target_score === null ? "none" : formatPercent(policy.target_score)}</dd>
                  <dt>Minimum improvement</dt>
                  <dd>{formatPp(policy.min_improvement * 100)}</dd>
                  <dt>Max category regression</dt>
                  <dd>{`${(policy.max_category_regression * 100).toFixed(1)} pp`}</dd>
                  <dt>New critical failures</dt>
                  <dd>{policy.allow_new_critical_failures ? "allowed" : "not allowed"}</dd>
                  <dt>Evaluation mode</dt>
                  <dd>{policy.eval_mode}</dd>
                  <dt>Holdout validation</dt>
                  <dd>{policy.run_holdout ? "on" : "off"}</dd>
                  <dt>Budgets</dt>
                  <dd>{Object.keys(policy.limits ?? {}).length ? <code>{JSON.stringify(policy.limits)}</code> : "none"}</dd>
                </dl>
              </details>
            )}
          </Card>

          {pending && <PendingChange key={pending.id} patch={pending} onDecided={reloadAndWatch} />}
          {data.status === "review_required" && !pending && (
            isActive(data.job?.status)
              ? <Notice>The decision was recorded. The run is resuming.</Notice>
              : <Notice tone="warn">This run is waiting for a decision, but the pending change could not be found. Use Refresh.</Notice>
          )}

          <Card title="Round-by-round timeline">
            <Timeline run={data} />
          </Card>

          <Card title="Paired A/B comparison on the regression set">
            <p className="muted">Baseline A against the best candidate version, case by case on the same regression cases.</p>
            {report.loading && !report.data && <Loading />}
            <ErrorNote error={report.error} />
            {report.data && !comparison && (
              <p className="muted">
                {data.baseline_run_id ? "No change has been kept, so there is no candidate version to compare with the baseline."
                  : "The baseline has not been evaluated yet."}
              </p>
            )}
            {comparison && (
              <>
                {comparison.synthetic && <p><SyntheticBadge /> <span className="muted small">Not a real evaluation.</span></p>}
                <PairedSummary overall={comparison.overall} />
                <div className="stat-row">
                  <Stat label="Pass rate" value={`${formatPercent(comparison.pass_rate.baseline)} → ${formatPercent(comparison.pass_rate.candidate)}`}
                    note="baseline → candidate" />
                  <Stat label="Unpaired cases" value={comparison.unpaired_cases} note="scored in only one of the two runs" />
                </div>
                <GroupDeltaTable caption="By category" groups={comparison.by_category} />
                <GroupDeltaTable caption="By dimension" groups={comparison.by_dimension} />
                <SafetyStats critical={comparison.critical_failures} latency={comparison.latency_ms} />
                {!comparison.conditions.same_dataset_version && (
                  <Notice tone="warn">The two runs used different versions of the dataset.</Notice>
                )}
              </>
            )}
          </Card>

          <HoldoutPanel holdout={data.holdout} finished={!stoppable} />

          <Card title="Usage">
            {report.data ? <UsageBoxes models={report.data.usage.platform_models} agent={report.data.usage.target_agent} />
              : <p className="muted">Usage is loading.</p>}
          </Card>

          <Card title="Caveats">
            {report.data ? (
              <ul className="caveats">
                {report.data.caveats.map((caveat, index) => <li key={index}>{caveat}</li>)}
                <li>{report.data.release_decision.note}</li>
              </ul>
            ) : <p className="muted">Caveats are loading.</p>}
            {data.status === "completed" && data.candidate_id && (
              <p>
                <Badge tone="info">Next step</Badge>{" "}
                <Link to={`/candidates/${data.candidate_id}`}>Review Candidate B and record a release decision</Link>
              </p>
            )}
          </Card>
        </>
      )}

      <ConfirmDialog open={confirmStop} title="Stop this optimization?" confirmLabel="Stop optimization" danger busy={stop.busy}
        onCancel={() => setConfirmStop(false)}
        onConfirm={() => void stop.run(async () => {
          try {
            await api.stopOptimization(id);
          } finally {
            setConfirmStop(false);
          }
          reloadAndWatch();
        })}>
        <p>
          The run stops where it is. The candidate keeps only changes that already passed every gate; a change that is being
          evaluated is rolled back.
        </p>
      </ConfirmDialog>
    </>
  );
}
