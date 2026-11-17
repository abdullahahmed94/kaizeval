import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { useSession } from "../auth";
import { Badge, Card, ErrorNote, Loading, PageHeader, Stat, StatusBadge, SyntheticBadge, SyntheticBanner, When } from "../components/ui";
import { formatPercent, formatPp, humanize } from "../format";
import { useLoad, useNames } from "../hooks";

interface Step {
  title: string;
  done: boolean;
  /** Set when the step is started but still needs attention. */
  attention?: boolean;
  status: ReactNode;
  to: string;
  action: string;
}

export function OverviewPage() {
  const { api, workspace } = useSession();
  const names = useNames();
  const state = useLoad(
    async () => {
      const [providers, policies, documents, specs, standalone, allRuns, optimizations, candidates] = await Promise.all([
        api.providers(),
        api.rolePolicies(),
        api.documents({ kind: "kb", limit: 200 }),
        api.specifications({ limit: 1 }),
        api.runs({ standalone: true, limit: 20 }),
        api.runs({ limit: 50 }),
        api.optimizations({ limit: 1 }),
        api.candidates(),
      ]);
      return { providers, policies, documents, specs, standalone, allRuns, optimizations, candidates };
    },
    [api],
  );

  const data = state.data;
  const steps: Step[] = [];
  if (data) {
    const verifiedProviders = data.providers.items.filter((provider) => provider.last_preflight?.ok);
    const readyRoles = data.policies.roles.filter((role) => role.ready).length;
    const anySyntheticRole = data.policies.roles.some((role) => role.ready && role.synthetic);
    steps.push({
      title: "Model verified",
      done: readyRoles > 0,
      attention: verifiedProviders.length > 0 && readyRoles === 0,
      status: data.providers.items.length === 0 ? "No model provider has been added." : (
        <>
          {verifiedProviders.length} of {data.providers.items.length} provider(s) passed preflight; {readyRoles} of{" "}
          {data.policies.roles.length} model roles are ready. {anySyntheticRole && <SyntheticBadge />}
        </>
      ),
      to: "/models",
      action: "Models",
    });

    const verifiedAgents = names.agents.filter((agent) => agent.verified);
    steps.push({
      title: "Agent verified",
      done: verifiedAgents.length > 0,
      attention: names.agents.length > 0 && verifiedAgents.length === 0,
      status: names.agents.length === 0 ? "No agent has been registered."
        : `${verifiedAgents.length} of ${names.agents.length} agent(s) verified.`,
      to: "/agents",
      action: "Agents",
    });

    const parsed = data.documents.items.filter((document) => document.status === "parsed");
    const failed = data.documents.items.filter((document) => document.status === "failed");
    steps.push({
      title: "Documents uploaded",
      done: parsed.length > 0,
      attention: failed.length > 0,
      status: data.documents.total === 0 ? "No knowledge document has been uploaded."
        : `${parsed.length} knowledge document(s) parsed${failed.length ? `, ${failed.length} failed` : ""}.`,
      to: "/knowledge",
      action: "Knowledge",
    });

    const spec = data.specs.items[0];
    steps.push({
      title: "Specification",
      done: Boolean(spec) && spec.status !== "draft",
      attention: Boolean(spec) && spec.status === "draft",
      status: !spec ? "No specification has been built." : (
        <>
          Latest: version {spec.version}, <StatusBadge status={spec.status} />{" "}
          {spec.status === "draft" && "waiting for Review & Edit, Approve or Skip & Continue. "}
          {spec.synthetic && <SyntheticBadge />}
        </>
      ),
      to: spec ? `/specifications/${spec.id}` : "/specifications",
      action: "Specification",
    });

    const ready = names.datasets.filter((dataset) => dataset.status === "ready");
    steps.push({
      title: "Dataset ready",
      done: ready.length > 0,
      attention: names.datasets.length > 0 && ready.length === 0,
      status: names.datasets.length === 0 ? "No test dataset exists."
        : `${ready.length} of ${names.datasets.length} dataset(s) ready.`,
      to: "/datasets",
      action: "Datasets",
    });

    const baseline = data.allRuns.items.find(
      (run) => run.status === "completed" && run.split === "regression" && run.kind === "baseline");
    steps.push({
      title: "Baseline evaluated",
      done: Boolean(baseline),
      status: baseline ? (
        <>
          Baseline scored {formatPercent(baseline.summary?.overall)} overall on <When value={baseline.created_at} />.{" "}
          {baseline.synthetic && <SyntheticBadge />}
        </>
      ) : "No completed baseline evaluation on a regression set.",
      to: baseline ? `/evaluations/${baseline.id}` : "/evaluations",
      action: "Evaluations",
    });

    const optimization = data.optimizations.items[0];
    const awaiting = data.candidates.items.filter((candidate) => candidate.status === "awaiting_decision");
    steps.push({
      title: "Optimization",
      done: Boolean(optimization) && optimization.status === "completed",
      attention: Boolean(optimization) && optimization.status === "review_required",
      status: !optimization ? "No optimization has been run." : (
        <>
          Latest run: <StatusBadge status={optimization.status} />
          {awaiting.length > 0 && ` ${awaiting.length} candidate(s) await a release decision.`}{" "}
          {optimization.synthetic && <SyntheticBadge />}
        </>
      ),
      to: optimization ? `/optimizations/${optimization.id}` : "/optimizations",
      action: "Optimization",
    });
  }

  const latestRun = data?.standalone.items[0] ?? data?.allRuns.items[0];
  const latestOptimization = data?.optimizations.items[0];

  return (
    <>
      <PageHeader title="Overview" subtitle={workspace ? `Workspace: ${workspace.name}` : undefined}
        actions={<button type="button" className="btn btn-small" onClick={() => { state.reload(); names.reload(); }}>Refresh</button>} />
      {(state.loading || names.loading) && !data && <Loading />}
      <ErrorNote error={state.error} />
      <ErrorNote error={names.error} />

      {data && (
        <Card title="Journey">
          <ol className="checklist">
            {steps.map((step) => (
              <li key={step.title} className={step.done ? "check-done" : step.attention ? "check-attention" : "check-todo"}>
                <span className="check-mark" aria-hidden="true" />
                <div className="check-body">
                  <p className="check-title">
                    {step.title}{" "}
                    <Badge tone={step.done ? "ok" : step.attention ? "warn" : "neutral"}>
                      {step.done ? "Done" : step.attention ? "Needs attention" : "Not started"}
                    </Badge>
                  </p>
                  <p className="muted">{step.status}</p>
                </div>
                <Link className="btn btn-small" to={step.to}>
                  {step.action}
                </Link>
              </li>
            ))}
          </ol>
        </Card>
      )}

      {data && (
        <div className="two-col">
          <Card title="Latest evaluation">
            {latestRun ? (
              <>
                <SyntheticBanner show={latestRun.synthetic} />
                <p>
                  <Link to={`/evaluations/${latestRun.id}`}>
                    {names.agentName(latestRun.agent_id)} on {names.datasetName(latestRun.dataset_id)}
                  </Link>{" "}
                  <StatusBadge status={latestRun.status} />
                </p>
                <p className="muted small">
                  {humanize(latestRun.split)} set, version {latestRun.agent_version}, {latestRun.mode} mode,{" "}
                  <When value={latestRun.created_at} />
                </p>
                {latestRun.summary ? (
                  <div className="stat-row">
                    <Stat label="Overall score" value={formatPercent(latestRun.summary.overall)} />
                    <Stat label="Pass rate" value={formatPercent(latestRun.summary.pass_rate)} />
                    <Stat label="Critical failures" value={latestRun.summary.critical_failures} />
                    <Stat label="Needs human review" value={latestRun.summary.needs_human_review} />
                  </div>
                ) : (
                  <p className="muted">No summary yet.</p>
                )}
                {latestRun.error && <p className="error-text">{latestRun.error}</p>}
              </>
            ) : (
              <p className="muted">
                No evaluation has been run. <Link to="/evaluations">Start one</Link> once an agent and a dataset are ready.
              </p>
            )}
          </Card>

          <Card title="Latest optimization">
            {latestOptimization ? (
              <>
                <SyntheticBanner show={latestOptimization.synthetic} />
                <p>
                  <Link to={`/optimizations/${latestOptimization.id}`}>
                    {names.agentName(latestOptimization.agent_id)} on {names.datasetName(latestOptimization.dataset_id)}
                  </Link>{" "}
                  <StatusBadge status={latestOptimization.status} />
                </p>
                <p className="muted small">
                  {humanize(latestOptimization.mode)}, <When value={latestOptimization.created_at} />
                  {latestOptimization.stop_reason ? `. Stopped: ${latestOptimization.stop_reason}.` : ""}
                </p>
                <div className="stat-row">
                  <Stat label="Baseline (regression)" value={formatPercent(latestOptimization.state.baseline_overall)} />
                  <Stat label="Best version (regression)" value={formatPercent(latestOptimization.state.best_overall)} />
                  <Stat label="Rounds" value={latestOptimization.state.round ?? 0} />
                  <Stat label="Holdout check"
                    value={latestOptimization.holdout?.status === "completed"
                      ? formatPp(latestOptimization.holdout.overall?.delta_percentage_points)
                      : humanize(latestOptimization.holdout?.status ?? "not run")}
                    note={latestOptimization.holdout?.status === "completed"
                      ? `verdict: ${latestOptimization.holdout.overall?.verdict}` : latestOptimization.holdout?.reason} />
                </div>
                {latestOptimization.candidate_id && (
                  <p>
                    <Link to={`/candidates/${latestOptimization.candidate_id}`}>Open the candidate and its release decision</Link>
                  </p>
                )}
                {latestOptimization.error && <p className="error-text">{latestOptimization.error}</p>}
              </>
            ) : (
              <p className="muted">
                No optimization has been run. <Link to="/optimizations">Start one</Link> after a baseline evaluation.
              </p>
            )}
          </Card>
        </div>
      )}
    </>
  );
}
