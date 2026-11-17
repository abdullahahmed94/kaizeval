import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useSession } from "../auth";
import {
  Badge, Card, Empty, ErrorNote, Field, JsonBlock, Loading, Notice, PageHeader, StatusBadge, SyntheticBadge, SyntheticBanner,
} from "../components/ui";
import { formatDate, formatPercent, humanize } from "../format";
import { useAction, useLoad, useNames } from "../hooks";
import type { Recommendation } from "../types";

function RecommendationCard({ item, showRun, onChanged }: { item: Recommendation; showRun: boolean; onChanged: () => void }) {
  const { api, can, userLabel } = useSession();
  const [reason, setReason] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const action = useAction();
  const reviewer = can("reviewer");

  const setState = (state: Recommendation["state"]) => {
    setLocalError(null);
    if (state === "rejected" && !reason.trim()) {
      setLocalError("Give a reason before rejecting a recommendation.");
      return;
    }
    void action.run(async () => {
      await api.setRecommendationState(item.id, state, reason.trim() || null);
      setReason("");
      onChanged();
    });
  };

  return (
    <li className="item">
      <div className="item-head">
        <div>
          <h3>
            {item.title} {item.synthetic && <SyntheticBadge />}
          </h3>
          <p className="muted small">
            {humanize(item.category)} · target: {item.target_component} · risk <StatusBadge status={item.risk} /> · priority {item.priority}
            {showRun && (
              <>
                {" "}· <Link to={`/evaluations/${item.run_id}`}>evaluation run</Link>
              </>
            )}
          </p>
        </div>
        <StatusBadge status={item.state} />
      </div>
      <p>{item.detail}</p>
      <dl className="kv">
        <dt>Impact</dt>
        <dd>
          {item.impact.cases_affected ?? 0} case(s) affected
          {item.impact.critical_cases ? `, ${item.impact.critical_cases} critical` : ""}
          {item.impact.share_of_cases_with_findings !== undefined
            ? ` (${formatPercent(item.impact.share_of_cases_with_findings)} of the cases with findings)` : ""}
          {item.impact.note && <span className="muted small"> — {item.impact.note}</span>}
        </dd>
        <dt>Automatic application</dt>
        <dd>
          {item.auto_apply_available ? (
            <>
              <Badge tone="ok">Available</Badge> An optimization run can try this change on an isolated candidate.{" "}
              <Link to="/optimizations">Go to optimization</Link>
            </>
          ) : (
            <>
              <Badge tone="warn">Not available</Badge> {item.auto_apply_blocker ?? "No reason was recorded."}
            </>
          )}
        </dd>
        <dt>Current state</dt>
        <dd>
          {humanize(item.state)}
          {item.state_by ? ` by ${userLabel(item.state_by)}` : ""}
          {item.state_reason ? ` — "${item.state_reason}"` : ""}
        </dd>
      </dl>
      {item.suggested_patch && (
        <>
          <p className="muted small">Suggested configuration patch</p>
          <JsonBlock value={item.suggested_patch} />
        </>
      )}
      {reviewer ? (
        <div className="form">
          <Field label="Reason (required to reject, optional otherwise)">
            <input type="text" value={reason} onChange={(event) => setReason(event.target.value)} maxLength={2000} />
          </Field>
          <div className="button-row">
            <button type="button" className="btn btn-small btn-primary" disabled={action.busy || item.state === "approved"} onClick={() => setState("approved")}>
              Approve
            </button>
            <button type="button" className="btn btn-small" disabled={action.busy || item.state === "deferred"} onClick={() => setState("deferred")}>
              Defer
            </button>
            <button type="button" className="btn btn-small btn-danger" disabled={action.busy || item.state === "rejected"} onClick={() => setState("rejected")}>
              Reject
            </button>
            <button type="button" className="btn btn-small" disabled={action.busy || item.state === "applied"} onClick={() => setState("applied")}>
              Mark applied
            </button>
          </div>
          {localError && <p className="error-text" role="alert">{localError}</p>}
          <ErrorNote error={action.error} />
        </div>
      ) : (
        <p className="muted small">Changing the state of a recommendation needs the reviewer role or higher.</p>
      )}
    </li>
  );
}

export function RecommendationsPage() {
  const { api, can } = useSession();
  const names = useNames();
  const [search, setSearch] = useSearchParams();
  const runId = search.get("run_id") ?? "";
  const runs = useLoad(() => api.runs({ limit: 100 }), [api]);
  const recommendations = useLoad(() => api.recommendations(runId || undefined), [api, runId]);
  const build = useAction();
  const data = recommendations.data;
  const eligible = (runs.data?.items ?? []).filter((run) => run.status === "completed" && run.split === "regression");
  const selected = eligible.find((run) => run.id === runId);

  return (
    <>
      <PageHeader title="Recommendations" subtitle="Prioritized changes derived from the findings of an evaluation run. Nothing is applied from this page." />
      <Card title="Evaluation run">
        <div className="filters">
          <Field label="Show recommendations for" hint="Only completed regression runs produce recommendations.">
            <select value={runId} onChange={(event) => setSearch(event.target.value ? { run_id: event.target.value } : {})}>
              <option value="">All runs in this workspace</option>
              {runId && !selected && <option value={runId}>Run {runId.slice(0, 8)}</option>}
              {eligible.map((run) => (
                <option key={run.id} value={run.id}>
                  {formatDate(run.created_at)} · {names.agentName(run.agent_id)} · {run.agent_version} · {formatPercent(run.summary?.overall)}
                  {run.synthetic ? " · SYNTHETIC" : ""}
                </option>
              ))}
            </select>
          </Field>
          {runId && can("editor") && (
            <button type="button" className="btn" disabled={build.busy}
              onClick={() => void build.run(async () => { await api.buildRecommendations(runId); recommendations.reload(); })}>
              {build.busy ? "Building…" : data && data.items.length > 0 ? "Rebuild recommendations" : "Build recommendations"}
            </button>
          )}
        </div>
        <ErrorNote error={runs.error} />
        <ErrorNote error={build.error} />
        {runId && (
          <p className="muted small">
            <Link to={`/evaluations/${runId}`}>Open the evaluation run</Link>. Rebuilding repeats the diagnosis and keeps decisions already taken.
          </p>
        )}
      </Card>

      {data && <SyntheticBanner show={data.items.some((item) => item.synthetic)} />}
      {recommendations.loading && !data && <Loading />}
      <ErrorNote error={recommendations.error} />
      {data && data.items.length === 0 && (
        <Card>
          <Empty title={runId ? "No recommendations for this run." : "No recommendations yet."}>
            {runId
              ? can("editor")
                ? "Use \"Build recommendations\" above. A run without failures produces none."
                : "An editor can build them for this run."
              : <>Run an <Link to="/evaluations">evaluation</Link>, then open its recommendations.</>}
          </Empty>
        </Card>
      )}
      {data && data.items.length > 0 && (
        <Card title={`Recommendations (${data.items.length}), highest priority first`}>
          <Notice>
            Impact counts how many cases show a problem; it indicates reach and is not a promised gain. Changes are only ever tried
            on an isolated candidate during an optimization run, never applied to production from here.
          </Notice>
          <ul className="item-list">
            {data.items.map((item) => (
              <RecommendationCard key={item.id} item={item} showRun={!runId} onChanged={recommendations.reload} />
            ))}
          </ul>
        </Card>
      )}
    </>
  );
}
