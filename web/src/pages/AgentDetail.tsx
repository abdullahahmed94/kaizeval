import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useSession } from "../auth";
import {
  Badge, Card, ConfirmDialog, ErrorNote, JsonBlock, Loading, Notice, PageHeader, StatusBadge, TableWrap, When,
} from "../components/ui";
import { formatValue, humanize } from "../format";
import { useAction, useLoad } from "../hooks";
import type { AgentCapabilities } from "../types";

const MODE_LABELS: Record<keyof AgentCapabilities["modes"], string> = {
  recommendations_only: "Evaluation and recommendations",
  human_guided_optimization: "Human-guided optimization",
  autonomous_optimization: "Autonomous optimization",
  promotion: "Promotion to production (after an explicit approval)",
};

function YesNo({ value }: { value: boolean }) {
  return <Badge tone={value ? "ok" : "neutral"}>{value ? "yes" : "no"}</Badge>;
}

function Configuration({ agentId }: { agentId: string }) {
  const { api } = useSession();
  const configuration = useLoad(() => api.agentConfiguration(agentId), [api, agentId]);
  const data = configuration.data;
  return (
    <Card title="Readable configuration (baseline)">
      {configuration.loading && !data && <Loading />}
      <ErrorNote error={configuration.error} />
      {data && (
        <>
          {data.description && <p>{data.description}</p>}
          {data.digest && (
            <p className="muted small">
              Configuration digest: <code>{data.digest}</code>
            </p>
          )}
          <TableWrap>
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Setting</th>
                  <th scope="col">Current value</th>
                  <th scope="col">Type</th>
                  <th scope="col">Allowed range</th>
                  <th scope="col">Description</th>
                  <th scope="col">Addresses</th>
                </tr>
              </thead>
              <tbody>
                {Object.keys({ ...data.config, ...data.schema }).map((key) => {
                  const meta = data.schema[key];
                  return (
                    <tr key={key}>
                      <th scope="row"><code>{key}</code></th>
                      <td><code>{formatValue(data.config[key])}</code></td>
                      <td>{meta?.type ?? <span className="muted">not patchable</span>}</td>
                      <td>{meta && meta.min !== undefined && meta.max !== undefined ? `${meta.min} to ${meta.max}` : "—"}</td>
                      <td>{meta?.description ?? "—"}</td>
                      <td>{meta?.addresses?.map(humanize).join(", ") ?? "—"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </TableWrap>
          <details>
            <summary>Patch schema (JSON)</summary>
            <JsonBlock value={data.schema} />
          </details>
        </>
      )}
    </Card>
  );
}

export function AgentDetailPage() {
  const { id = "" } = useParams();
  const { api, can } = useSession();
  const navigate = useNavigate();
  const agent = useLoad(() => api.agent(id), [api, id]);
  const capabilities = useLoad(() => api.agentCapabilities(id), [api, id]);
  const adapters = useLoad(() => api.adapters(), [api]);
  const verify = useAction();
  const remove = useAction();
  const [confirming, setConfirming] = useState(false);

  const data = agent.data;
  const caps = capabilities.data;
  const reload = () => {
    agent.reload();
    capabilities.reload();
  };

  return (
    <>
      <PageHeader title={data ? data.name : "Agent"} subtitle={<Link to="/agents">All agents</Link>}
        actions={
          <>
            {can("editor") && (
              <button type="button" className="btn btn-primary" disabled={verify.busy || !data}
                onClick={() => void verify.run(async () => {
                  try {
                    await api.verifyAgent(id);
                  } finally {
                    reload();
                  }
                })}>
                {verify.busy ? "Verifying…" : "Verify connection"}
              </button>
            )}
            {can("owner") && (
              <button type="button" className="btn btn-danger" disabled={!data || remove.busy} onClick={() => setConfirming(true)}>
                Delete
              </button>
            )}
          </>
        } />
      {agent.loading && !data && <Loading />}
      <ErrorNote error={agent.error} />
      <ErrorNote error={verify.error} />
      <ErrorNote error={remove.error} />

      {data && (
        <Card title="Connection">
          <dl className="kv">
            <dt>Adapter</dt>
            <dd>{adapters.data?.adapters[data.adapter]?.label ?? data.adapter}</dd>
            <dt>Endpoint</dt>
            <dd><code>{formatValue(data.config.base_url ?? data.config.url)}</code></dd>
            <dt>Credential</dt>
            <dd>{data.has_credential ? "stored (encrypted)" : "none"}</dd>
            <dt>Access level granted</dt>
            <dd>{humanize(data.access_level)}</dd>
            <dt>Promotion granted</dt>
            <dd>{data.allow_promote ? "yes" : "no"}</dd>
            <dt>Status</dt>
            <dd>
              {data.verified ? (
                <>
                  <StatusBadge status="verified" /> <When value={data.verified_at} />
                </>
              ) : (
                <Badge tone="warn">Not verified</Badge>
              )}
            </dd>
          </dl>
          {adapters.data?.adapters[data.adapter] && <p className="muted">{adapters.data.adapters[data.adapter].description}</p>}
          {data.last_verification && !data.last_verification.ok && (
            <div className="alert alert-error" role="alert">
              <p>
                Last verification failed: {data.last_verification.message}{" "}
                <code className="error-code">{data.last_verification.code}</code>
              </p>
            </div>
          )}
          {data.last_verification?.ok && data.last_verification.healthcheck && (
            <p className="muted small">
              Last health check: <code>{JSON.stringify(data.last_verification.healthcheck)}</code>
            </p>
          )}
          {!data.verified && !data.last_verification && (
            <Notice tone="warn">
              This agent has not been verified. Use "Verify connection" to probe it and record which capabilities are available.
            </Notice>
          )}
          <details>
            <summary>Connection settings (JSON)</summary>
            <JsonBlock value={data.config} />
          </details>
        </Card>
      )}

      <Card title="Capabilities">
        {capabilities.loading && !caps && <Loading />}
        <ErrorNote error={capabilities.error} />
        {caps && (
          <>
            {caps.explanation && (
              <Notice tone="warn" title="Optimization is not available for this agent">
                <p>{caps.explanation}</p>
                {data && (
                  <p>
                    <Link to="/evaluations">Run an evaluation</Link> and use its{" "}
                    <Link to="/recommendations">recommendations</Link> instead.
                  </p>
                )}
              </Notice>
            )}
            {!caps.verified && (
              <Notice tone="warn">
                The agent has not been verified, so the adapter's capabilities are not known yet and every row shows as
                unavailable. Verify the connection to fill in this matrix.
              </Notice>
            )}
            <TableWrap>
              <table className="data">
                <caption>
                  A capability is available only when the adapter implements it, you granted it, and the last verification confirmed it.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Capability</th>
                    <th scope="col">Supported by adapter</th>
                    <th scope="col">Granted</th>
                    <th scope="col">Available</th>
                    <th scope="col">Reason when unavailable</th>
                  </tr>
                </thead>
                <tbody>
                  {caps.matrix.map((row) => (
                    <tr key={row.capability}>
                      <th scope="row">{humanize(row.capability)}</th>
                      <td><YesNo value={row.supported_by_adapter} /></td>
                      <td><YesNo value={row.granted} /></td>
                      <td><Badge tone={row.available ? "ok" : "bad"}>{row.available ? "available" : "unavailable"}</Badge></td>
                      <td>
                        {row.available ? "—" : row.reason
                          ?? (caps.verified ? "Not confirmed by the last verification."
                            : "The agent has not been verified yet; verify the connection to confirm this capability.")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </TableWrap>
            <h3>Available modes</h3>
            <ul className="check-results">
              {(Object.keys(MODE_LABELS) as (keyof AgentCapabilities["modes"])[]).map((mode) => (
                <li key={mode}>
                  <Badge tone={caps.modes[mode] ? "ok" : "neutral"}>{caps.modes[mode] ? "available" : "not available"}</Badge>
                  <span>{MODE_LABELS[mode]}</span>
                </li>
              ))}
            </ul>
          </>
        )}
      </Card>

      {caps && caps.capabilities.includes("inspect") ? (
        <Configuration agentId={id} />
      ) : (
        caps && (
          <Card title="Readable configuration">
            <p className="muted">
              The configuration of this agent is not readable{caps.verified ? " with the current adapter and access level" : " until the connection is verified"}.
            </p>
          </Card>
        )
      )}

      <ConfirmDialog open={confirming} title="Delete agent" confirmLabel="Delete agent" danger busy={remove.busy}
        onCancel={() => setConfirming(false)}
        onConfirm={() => void remove.run(async () => {
          try {
            await api.deleteAgent(id);
          } finally {
            setConfirming(false);
          }
          navigate("/agents");
        })}>
        <p>
          Delete <strong>{data?.name}</strong>? Agents with evaluation history cannot be deleted.
        </p>
      </ConfirmDialog>
    </>
  );
}
