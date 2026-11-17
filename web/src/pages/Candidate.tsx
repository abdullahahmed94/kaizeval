import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useSession } from "../auth";
import {
  Badge, Card, ConfirmDialog, DiffBlock, ErrorNote, Field, Loading, Notice, PageHeader, StatusBadge, SyntheticBadge,
  SyntheticBanner, TableWrap, When,
} from "../components/ui";
import { formatPercent, formatPp, formatValue, humanize } from "../format";
import { useAction, useLoad, useNames } from "../hooks";
import type { Candidate, Dict } from "../types";

function ConfigCompare({ baseline, candidate }: { baseline: Dict; candidate: Dict }) {
  const keys = Array.from(new Set([...Object.keys(baseline), ...Object.keys(candidate)])).sort();
  return (
    <TableWrap>
      <table className="data">
        <thead>
          <tr>
            <th scope="col">Setting</th>
            <th scope="col">Baseline A</th>
            <th scope="col">Candidate B</th>
            <th scope="col">Changed</th>
          </tr>
        </thead>
        <tbody>
          {keys.map((key) => {
            const changed = JSON.stringify(baseline[key]) !== JSON.stringify(candidate[key]);
            return (
              <tr key={key} className={changed ? "row-changed" : undefined}>
                <th scope="row"><code>{key}</code></th>
                <td><code>{key in baseline ? formatValue(baseline[key]) : "unset"}</code></td>
                <td><code>{key in candidate ? formatValue(candidate[key]) : "unset"}</code></td>
                <td>{changed ? <Badge tone="info">changed</Badge> : <span className="muted">same</span>}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </TableWrap>
  );
}

function ReleaseDecision({ candidate, onChanged }: { candidate: Candidate; onChanged: () => void }) {
  const { api, can, userLabel } = useSession();
  const owner = can("owner");
  const report = useLoad(
    async () => (candidate.optimization_run_id ? api.optimizationReport(candidate.optimization_run_id) : null),
    [api, candidate.optimization_run_id],
  );
  const capabilities = useLoad(() => api.agentCapabilities(candidate.agent_id), [api, candidate.agent_id]);
  const [note, setNote] = useState("");
  const [confirm, setConfirm] = useState<"promote" | "rollback" | null>(null);
  const decide = useAction();
  const release = useAction();

  const approvals = report.data?.release_decision.approvals ?? [];
  const openApproval = approvals.find((approval) => approval.decision === "approved" && !approval.consumed);
  const canPromote = capabilities.data?.modes.promotion;

  const changed = () => {
    report.reload();
    onChanged();
  };

  const record = (decision: "approved" | "rejected") =>
    void decide.run(async () => {
      await api.decideRelease(candidate.id, decision, note.trim() || null);
      setNote("");
      changed();
    });

  const act = () => {
    const kind = confirm;
    if (!kind) return;
    void release.run(async () => {
      try {
        if (kind === "promote") await api.promoteCandidate(candidate.id);
        else await api.rollbackCandidate(candidate.id, note.trim() || null);
      } finally {
        setConfirm(null);
      }
      setNote("");
      changed();
    });
  };

  return (
    <Card title="Release decision">
      <Notice title="Nothing is promoted automatically">
        <p>
          Kaizeval never changes production on its own. Releasing Candidate B takes two separate, explicit steps by an owner:
          first record an approval, then promote. Approval alone deploys nothing, and production can be rolled back to the
          untouched baseline afterwards.
        </p>
      </Notice>
      <p>
        Candidate status: <StatusBadge status={candidate.status} />{" "}
        {candidate.status === "awaiting_decision" && (openApproval
          ? <Badge tone="ok">Release approved, not yet promoted</Badge>
          : <Badge tone="warn">No release approval recorded</Badge>)}
      </p>

      <h3>Recorded decisions</h3>
      {report.loading && !report.data && <Loading />}
      <ErrorNote error={report.error} />
      {report.data !== undefined && approvals.length === 0 && <p className="muted">No release decision has been recorded.</p>}
      {approvals.length > 0 && (
        <TableWrap>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Decision</th>
                <th scope="col">By</th>
                <th scope="col">When</th>
                <th scope="col">Note</th>
                <th scope="col">Used for a promotion</th>
              </tr>
            </thead>
            <tbody>
              {approvals.map((approval, index) => {
                // The server records a production rollback as an approval whose note starts with "rollback:".
                const rollback = (approval.note ?? "").startsWith("rollback:");
                const note = rollback ? (approval.note ?? "").slice(9).trim() : approval.note;
                return (
                  <tr key={index}>
                    <th scope="row">{rollback ? <Badge tone="warn">Rollback</Badge> : <StatusBadge status={approval.decision} />}</th>
                    <td>{userLabel(approval.by)}</td>
                    <td><When value={approval.at} /></td>
                    <td>{note || "—"}</td>
                    <td>{rollback ? "—" : approval.consumed ? "yes" : "not yet"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableWrap>
      )}

      {!owner && <p className="muted small">Release decisions, promotion and rollback need the owner role.</p>}
      {owner && (candidate.status === "awaiting_decision" || candidate.status === "promoted") && (
        <Field label={candidate.status === "promoted" ? "Rollback note (optional)" : "Decision note (optional)"}>
          <input type="text" value={note} onChange={(event) => setNote(event.target.value)} maxLength={2000} />
        </Field>
      )}

      {owner && candidate.status === "awaiting_decision" && (
        <>
          <h3>Step 1: record the decision</h3>
          <div className="button-row">
            <button type="button" className="btn btn-primary" disabled={decide.busy || Boolean(openApproval)} onClick={() => record("approved")}>
              Approve release
            </button>
            <button type="button" className="btn btn-danger" disabled={decide.busy} onClick={() => record("rejected")}>
              Reject
            </button>
          </div>
          {openApproval && <p className="muted small">An approval is already recorded. You can still reject the release instead.</p>}
          <ErrorNote error={decide.error} />

          <h3>Step 2: promote</h3>
          {canPromote === false && (
            <Notice tone="warn">
              Promotion through the connector is not available for this agent (it was not granted, or the adapter does not support
              it). After approval, release the candidate with your own deployment process. The server explains the refusal if you try.
            </Notice>
          )}
          <div className="button-row">
            <button type="button" className="btn" disabled={release.busy || !openApproval} onClick={() => setConfirm("promote")}>
              Promote to production
            </button>
            {!openApproval && <span className="muted small">Available after "Approve release" has been recorded.</span>}
          </div>
        </>
      )}

      {owner && candidate.status === "promoted" && (
        <>
          <p>This candidate is in production.</p>
          <div className="button-row">
            <button type="button" className="btn btn-danger" disabled={release.busy} onClick={() => setConfirm("rollback")}>
              Roll back production
            </button>
          </div>
        </>
      )}
      {candidate.status === "rejected" && <p className="muted">This candidate was rejected and cannot be promoted.</p>}
      {candidate.status === "rolled_back" && <p className="muted">Production was rolled back to the baseline. This candidate is no longer in use.</p>}
      {candidate.status === "active" && <p className="muted">The optimization that owns this candidate has not finished yet.</p>}
      <ErrorNote error={release.error} />
      <ErrorNote error={capabilities.error} />

      <ConfirmDialog open={confirm !== null} danger busy={release.busy}
        title={confirm === "rollback" ? "Roll back production?" : "Promote Candidate B to production?"}
        confirmLabel={confirm === "rollback" ? "Roll back production" : "Promote to production"}
        onCancel={() => setConfirm(null)} onConfirm={act}>
        {confirm === "rollback" ? (
          <p>Production will point back at the untouched baseline configuration. This is recorded in the audit log.</p>
        ) : (
          <p>
            The agent's production version will switch to candidate <code>{candidate.external_id}</code> through its connector, using
            the recorded approval. This is recorded in the audit log and can be rolled back.
          </p>
        )}
      </ConfirmDialog>
    </Card>
  );
}

export function CandidatePage() {
  const { id = "" } = useParams();
  const { api, userLabel } = useSession();
  const names = useNames();
  const candidate = useLoad(() => api.candidate(id), [api, id]);
  const data = candidate.data;
  const synthetic = data?.patches?.some((patch) => patch.synthetic);

  return (
    <>
      <PageHeader title={data ? `Candidate ${data.external_id}` : "Candidate"}
        subtitle={data?.optimization_run_id
          ? <Link to={`/optimizations/${data.optimization_run_id}`}>Back to the optimization run</Link>
          : <Link to="/optimizations">Optimization</Link>}
        actions={<button type="button" className="btn btn-small" onClick={candidate.reload}>Refresh</button>} />
      {candidate.loading && !data && <Loading />}
      <ErrorNote error={candidate.error} />
      {data && (
        <>
          <SyntheticBanner show={synthetic}
            detail="The changes in this candidate were proposed by the fake provider; its measured gains are not real quality." />
          <Card title="Candidate">
            <dl className="kv">
              <dt>Status</dt>
              <dd><StatusBadge status={data.status} /></dd>
              <dt>Agent</dt>
              <dd><Link to={`/agents/${data.agent_id}`}>{names.agentName(data.agent_id)}</Link></dd>
              <dt>Candidate version</dt>
              <dd><code>{data.external_id}</code></dd>
              <dt>Baseline digest</dt>
              <dd><code className="wrap">{data.baseline_digest || "—"}</code></dd>
              <dt>Created</dt>
              <dd><When value={data.created_at} /></dd>
            </dl>
          </Card>

          <Card title="Baseline A and Candidate B">
            <p className="muted">
              Baseline A is the configuration that was in place when the optimization started; it has not been modified. Candidate B
              is an isolated copy that holds only the changes that passed every gate.
            </p>
            <ConfigCompare baseline={data.baseline_snapshot} candidate={data.config_snapshot} />
            <h3>Diff from the baseline</h3>
            <DiffBlock diff={data.diff_from_baseline} />
          </Card>

          <Card title="Patch history">
            {(data.patches ?? []).length === 0 ? <p className="muted">No change has been proposed for this candidate.</p> : (
              <TableWrap>
                <table className="data">
                  <thead>
                    <tr>
                      <th scope="col">Round</th>
                      <th scope="col">Patch</th>
                      <th scope="col">State</th>
                      <th scope="col">Risk</th>
                      <th scope="col" className="num">Score</th>
                      <th scope="col" className="num">Change against baseline</th>
                      <th scope="col">Rationale and decision</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(data.patches ?? []).map((patch) => (
                      <tr key={patch.id}>
                        <th scope="row">{patch.round} {patch.synthetic && <SyntheticBadge />}</th>
                        <td><code className="nowrap">{JSON.stringify(patch.patch)}</code></td>
                        <td><StatusBadge status={patch.state} /></td>
                        <td><StatusBadge status={patch.risk} /></td>
                        <td className="num">{formatPercent(patch.outcome?.overall)}</td>
                        <td className="num">{formatPp(patch.outcome?.delta_vs_baseline_pp)}</td>
                        <td className="cell-wide">
                          <p>{patch.rationale || "—"}</p>
                          {(patch.decided_by || patch.decision_note) && (
                            <p className="muted small">
                              {patch.decided_by ? `Decided by ${userLabel(patch.decided_by)}. ` : ""}
                              {patch.decision_note ?? ""}
                            </p>
                          )}
                          {patch.outcome?.reasons && patch.outcome.reasons.length > 0 && (
                            <p className="muted small">{humanize(patch.state)}: {patch.outcome.reasons.join("; ")}</p>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </TableWrap>
            )}
          </Card>

          <ReleaseDecision candidate={data} onChanged={candidate.reload} />
        </>
      )}
    </>
  );
}
