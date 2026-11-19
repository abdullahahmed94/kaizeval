import { useState, type FormEvent } from "react";

import { system, type RolePolicyInput } from "../api";
import { useSession } from "../auth";
import { UsageTable } from "../components/UsageBoxes";
import {
  Badge, Card, Check, ConfirmDialog, Empty, ErrorNote, Field, Loading, Notice, PageHeader, SyntheticBadge, TableWrap, When,
} from "../components/ui";
import { humanize } from "../format";
import { useAction, useLoad, type LoadState } from "../hooks";
import type { Dict, Preflight, Provider, RolePolicies, RolePolicy, RoleRouting } from "../types";

const FAKE_LABEL = "Fake provider — SYNTHETIC, for demos and CI only";

function PreflightChecks({ result }: { result: Preflight }) {
  return (
    <div className="preflight">
      <p>
        Preflight <Badge tone={result.ok ? "ok" : "bad"}>{result.ok ? "passed" : "failed"}</Badge>{" "}
        <span className="muted small">
          checked <When value={result.checked_at} />, workspace {result.offline ? "offline (local only)" : "connected"}
        </span>{" "}
        {result.synthetic && <SyntheticBadge />}
      </p>
      <ul className="check-results">
        {result.checks.map((check) => (
          <li key={check.name}>
            <Badge tone={check.ok ? "ok" : "bad"}>{check.ok ? "pass" : "fail"}</Badge>
            <span>
              <strong>{humanize(check.name)}</strong>: {check.detail}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ProviderRow({ provider, onChanged }: { provider: Provider; onChanged: () => void }) {
  const { api, can } = useSession();
  const verify = useAction();
  const remove = useAction();
  const [confirming, setConfirming] = useState(false);

  return (
    <li className="item">
      <div className="item-head">
        <div>
          <h3>
            {provider.name} {provider.synthetic && <SyntheticBadge />}
          </h3>
          <p className="muted small">
            {provider.kind === "fake" ? FAKE_LABEL : (
              <>
                {provider.kind === "aws_bedrock"
                  ? <>Amazon Bedrock{provider.params?.region ? `, region ${String(provider.params.region)}` : ""}{provider.base_url ? <> at <code>{provider.base_url}</code></> : null}</>
                  : <>OpenAI-compatible endpoint <code>{provider.base_url}</code></>}
                {provider.default_model ? `, default model ${provider.default_model}` : ""}
                {provider.has_credential ? ", credential stored" : ", no stored credential"}
                {provider.allow_egress ? ", public endpoint allowed" : ""}
              </>
            )}
          </p>
        </div>
        <div className="button-row">
          {can("editor") && (
            <button type="button" className="btn btn-small" disabled={verify.busy}
              onClick={() => void verify.run(async () => { await api.verifyProvider(provider.id); onChanged(); })}>
              {verify.busy ? "Verifying…" : "Verify"}
            </button>
          )}
          {can("owner") && (
            <button type="button" className="btn btn-small btn-danger" disabled={remove.busy} onClick={() => setConfirming(true)}>
              Delete
            </button>
          )}
        </div>
      </div>
      <ErrorNote error={verify.error} />
      <ErrorNote error={remove.error} />
      {provider.last_preflight ? (
        <PreflightChecks result={provider.last_preflight} />
      ) : (
        <p className="muted">Not verified yet. AI-assisted steps stay blocked until this provider passes preflight.</p>
      )}
      {Object.keys(provider.params ?? {}).length > 0 && (
        <p className="muted small">
          Parameters: <code>{JSON.stringify(provider.params)}</code>
        </p>
      )}
      <ConfirmDialog open={confirming} title="Delete model provider" confirmLabel="Delete provider" danger busy={remove.busy}
        onCancel={() => setConfirming(false)}
        onConfirm={() => void remove.run(async () => {
          try {
            await api.deleteProvider(provider.id);
          } finally {
            setConfirming(false);
          }
          onChanged();
        })}>
        <p>
          Delete <strong>{provider.name}</strong>? Role assignments that use it are removed as well, and its stored API key is erased.
        </p>
      </ConfirmDialog>
    </li>
  );
}

function AddProvider({ kinds, onCreated }: { kinds: string[]; onCreated: () => void }) {
  const { api, workspace } = useSession();
  const [kind, setKind] = useState("openai_compatible");
  const [name, setName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [allowEgress, setAllowEgress] = useState(false);
  const [params, setParams] = useState("");
  const [region, setRegion] = useState("");
  const [paramsError, setParamsError] = useState<string | null>(null);
  const action = useAction();
  const bedrock = kind === "aws_bedrock";

  const submit = (event: FormEvent) => {
    event.preventDefault();
    let parsed: Dict = {};
    setParamsError(null);
    if (kind !== "fake" && params.trim()) {
      try {
        const value: unknown = JSON.parse(params);
        if (typeof value !== "object" || value === null || Array.isArray(value)) throw new Error("not an object");
        parsed = value as Dict;
      } catch {
        setParamsError("Parameters must be a JSON object, for example {\"context_window\": 8192}.");
        return;
      }
    }
    void action.run(async () => {
      await api.createProvider(
        kind === "fake"
          ? { name: name.trim(), kind }
          : {
              name: name.trim(), kind, base_url: baseUrl.trim() || null, api_key: apiKey || null,
              default_model: defaultModel.trim(), allow_egress: allowEgress,
              params: bedrock ? { ...parsed, region: region.trim() } : parsed,
            },
      );
      setName(""); setBaseUrl(""); setApiKey(""); setDefaultModel(""); setAllowEgress(false); setParams(""); setRegion("");
      onCreated();
    });
  };

  return (
    <Card title="Add a model provider">
      <form onSubmit={submit} className="form">
        <div className="form-grid">
          <Field label="Kind">
            <select value={kind} onChange={(event) => setKind(event.target.value)}>
              {kinds.map((item) => (
                <option key={item} value={item}>
                  {item === "fake" ? FAKE_LABEL : item === "openai_compatible" ? "OpenAI-compatible endpoint"
                    : item === "aws_bedrock" ? "Amazon Bedrock" : item}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Name">
            <input type="text" value={name} onChange={(event) => setName(event.target.value)} required maxLength={200} />
          </Field>
        </div>
        {kind === "fake" ? (
          <Notice tone="warn" title={FAKE_LABEL}>
            <p>
              The fake provider runs in-process and returns deterministic, template-based output. Everything produced
              with it is labelled SYNTHETIC and is not evidence of real agent quality.
            </p>
          </Notice>
        ) : (
          <>
            {bedrock ? (
              <div className="form-grid">
                <Field label="Region" hint="The AWS Region where the model is enabled, for example us-east-1.">
                  <input type="text" value={region} onChange={(event) => setRegion(event.target.value)} required maxLength={30}
                    pattern="[a-z]{2}(-[a-z]+)+-[0-9]{1,2}" />
                </Field>
                <Field label="Model" hint="A model id or an inference profile id, exactly as Amazon Bedrock lists it.">
                  <input type="text" value={defaultModel} onChange={(event) => setDefaultModel(event.target.value)} required maxLength={200} />
                </Field>
                <Field label="AWS credential"
                  hint="ACCESS_KEY_ID:SECRET_ACCESS_KEY (add :SESSION_TOKEN for temporary credentials), or a Bedrock API key. Stored encrypted; never shown again. Leave empty to use the AWS environment variables of the Kaizeval server.">
                  <input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off" />
                </Field>
                <Field label="Endpoint URL (optional)" hint="Only for a private (VPC) endpoint. Empty uses the public endpoint of the Region.">
                  <input type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} maxLength={500} />
                </Field>
              </div>
            ) : (
              <div className="form-grid">
                <Field label="Base URL" hint="For example http://localhost:11434/v1">
                  <input type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} required maxLength={500} />
                </Field>
                <Field label="API key (optional)" hint="Stored encrypted; it is never shown again.">
                  <input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} autoComplete="off" />
                </Field>
                <Field label="Default model" hint="Model name sent to the endpoint when a role does not set its own.">
                  <input type="text" value={defaultModel} onChange={(event) => setDefaultModel(event.target.value)} maxLength={200} />
                </Field>
              </div>
            )}
            <Check label="Allow sending data to a public endpoint" checked={allowEgress} onChange={setAllowEgress} />
            {allowEgress && (
              <Notice tone="warn" title="Workspace content will leave your network">
                <p>
                  With this option, document passages, test questions and agent answers are sent to the endpoint above
                  when it is not local. Enable it only for a provider you are allowed to share this data with.
                  {workspace?.effective_offline
                    ? " This workspace is offline (local only), so a public endpoint will still be refused."
                    : ""}
                </p>
              </Notice>
            )}
            <Field label="Parameters (optional JSON)"
              hint="Recognised keys include context_window, json_mode, max_retries, max_concurrency, price_per_1k_input and price_per_1k_output.">
              <textarea rows={3} value={params} onChange={(event) => setParams(event.target.value)} spellCheck={false}
                placeholder='{"context_window": 8192}' />
            </Field>
            {paramsError && <p className="error-text" role="alert">{paramsError}</p>}
          </>
        )}
        <ErrorNote error={action.error} />
        <div className="button-row">
          <button type="submit" className="btn btn-primary" disabled={action.busy}>
            {action.busy ? "Adding…" : "Add provider"}
          </button>
        </div>
      </form>
    </Card>
  );
}

function PolicyEditor({ role, current, providers, onDone, onCancel }: {
  role: string; current: RolePolicy | null; providers: Provider[]; onDone: () => void; onCancel: () => void;
}) {
  const { api } = useSession();
  const [providerId, setProviderId] = useState(current?.provider_id ?? providers[0]?.id ?? "");
  const [model, setModel] = useState(current?.model ?? "");
  const [temperature, setTemperature] = useState(String(current?.temperature ?? 0));
  const [maxTokens, setMaxTokens] = useState(String(current?.max_tokens ?? 2000));
  const [timeout, setTimeoutValue] = useState(String(current?.timeout_s ?? 120));
  const action = useAction();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const body: RolePolicyInput = {
      provider_id: providerId, model: model.trim(), temperature: Number(temperature),
      max_tokens: Number(maxTokens), timeout_s: Number(timeout),
    };
    void action.run(async () => {
      await api.setRolePolicy(role, body);
      onDone();
    });
  };

  return (
    <form onSubmit={submit} className="form policy-editor" aria-label={`Model assignment for ${humanize(role)}`}>
      <div className="form-grid">
        <Field label="Provider">
          <select value={providerId} onChange={(event) => setProviderId(event.target.value)} required>
            {providers.map((provider) => (
              <option key={provider.id} value={provider.id}>
                {provider.name}{provider.synthetic ? " (SYNTHETIC)" : ""}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Model" hint="Leave empty to use the provider's default model.">
          <input type="text" value={model} onChange={(event) => setModel(event.target.value)} maxLength={200} />
        </Field>
        <Field label="Temperature">
          <input type="number" value={temperature} min={0} max={2} step={0.1} onChange={(event) => setTemperature(event.target.value)} required />
        </Field>
        <Field label="Max tokens">
          <input type="number" value={maxTokens} min={16} max={64000} step={1} onChange={(event) => setMaxTokens(event.target.value)} required />
        </Field>
        <Field label="Timeout (seconds)">
          <input type="number" value={timeout} min={1} max={1800} step={1} onChange={(event) => setTimeoutValue(event.target.value)} required />
        </Field>
      </div>
      <ErrorNote error={action.error} />
      <div className="button-row">
        <button type="submit" className="btn btn-primary btn-small" disabled={action.busy || !providerId}>
          {action.busy ? "Saving…" : "Save"}
        </button>
        <button type="button" className="btn btn-small" onClick={onCancel} disabled={action.busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function RoleRoutingTable({ providers, policies }: { providers: Provider[]; policies: LoadState<RolePolicies> }) {
  const { api, can } = useSession();
  const [editing, setEditing] = useState<string | null>(null);
  const clear = useAction();
  const editor = can("editor");
  const providerName = (id: string | null | undefined) =>
    id ? providers.find((provider) => provider.id === id)?.name ?? "deleted provider" : "—";

  const done = () => {
    setEditing(null);
    policies.reload();
  };

  const data = policies.data;
  const rows: { role: string; own: RolePolicy | null; routing: RoleRouting | null }[] = data
    ? [{ role: "default", own: data.default, routing: null },
       ...data.roles.map((routing) => ({ role: routing.role, own: routing.policy, routing }))]
    : [];

  return (
    <Card title="Role routing">
      <p className="muted">
        Each AI-assisted step runs under a role. A role uses its own assignment when one is set, otherwise the default model.
        A role is ready only when its provider has passed preflight.
      </p>
      {policies.loading && !data && <Loading />}
      <ErrorNote error={policies.error} />
      <ErrorNote error={clear.error} />
      {data && (
        <TableWrap>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Role</th>
                <th scope="col">Effective provider</th>
                <th scope="col">Model</th>
                <th scope="col">Assignment</th>
                <th scope="col">Status</th>
                {editor && <th scope="col">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map(({ role, own, routing }) => {
                const effective = own ?? (routing?.inherits_default ? data.default : null);
                return (
                  <tr key={role} className={role === "default" ? "row-strong" : undefined}>
                    <th scope="row">{role === "default" ? "Default model" : humanize(role)}</th>
                    <td>
                      {providerName(routing ? routing.effective_provider_id : own?.provider_id)}{" "}
                      {(routing ? routing.synthetic : providers.find((provider) => provider.id === own?.provider_id)?.synthetic) && (
                        <SyntheticBadge />
                      )}
                    </td>
                    <td>
                      {effective ? (
                        <>
                          {effective.model || <span className="muted">provider default</span>}
                          <span className="muted small">
                            {" "}· temperature {effective.temperature}, max {effective.max_tokens} tokens, {effective.timeout_s}s timeout
                          </span>
                        </>
                      ) : "—"}
                    </td>
                    <td>
                      {own ? (role === "default" ? "set" : "own override")
                        : routing?.inherits_default ? "inherits default" : <span className="muted">not set</span>}
                    </td>
                    <td>
                      {routing ? (
                        <Badge tone={routing.ready ? "ok" : "bad"}>{routing.ready ? "ready" : "not ready"}</Badge>
                      ) : own ? <Badge tone="info">in use as fallback</Badge> : <Badge tone="warn">no default</Badge>}
                    </td>
                    {editor && (
                      <td>
                        <div className="button-row">
                          <button type="button" className="btn btn-small" disabled={providers.length === 0}
                            onClick={() => setEditing(editing === role ? null : role)} aria-expanded={editing === role}>
                            {own ? "Edit" : role === "default" ? "Set default" : "Set override"}
                          </button>
                          {own && (
                            <button type="button" className="btn btn-small" disabled={clear.busy}
                              onClick={() => void clear.run(async () => { await api.clearRolePolicy(role); done(); })}>
                              Clear
                            </button>
                          )}
                        </div>
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </TableWrap>
      )}
      {editing && data && (
        <div className="box">
          <h3>{editing === "default" ? "Default model" : `Override for ${humanize(editing)}`}</h3>
          <PolicyEditor key={editing} role={editing} providers={providers}
            current={editing === "default" ? data.default : data.roles.find((item) => item.role === editing)?.policy ?? null}
            onDone={done} onCancel={() => setEditing(null)} />
        </div>
      )}
      {editor && providers.length === 0 && <p className="muted small">Add a provider before assigning models to roles.</p>}
    </Card>
  );
}

export function ModelsPage() {
  const { api, can } = useSession();
  const providers = useLoad(() => api.providers(), [api]);
  const usage = useLoad(() => api.modelUsage(), [api]);
  const policies = useLoad(() => api.rolePolicies(), [api]);
  const info = useLoad(() => system.info(), []);
  // Offer the real endpoint kind first; the fake provider stays available but is never the default.
  const kinds = [...(info.data?.provider_kinds ?? ["openai_compatible", "fake"])].sort(
    (a, b) => Number(a === "fake") - Number(b === "fake"));

  const providersChanged = () => {
    providers.reload();
    policies.reload();
  };

  return (
    <>
      <PageHeader title="Models"
        subtitle="Models that Kaizeval itself uses to build specifications, generate tests, judge answers and propose changes. The agent under test is configured separately." />

      <Card title="Providers">
        {providers.loading && !providers.data && <Loading />}
        <ErrorNote error={providers.error} />
        {providers.data && providers.data.items.length === 0 && (
          <Empty title="No model provider yet.">
            {can("owner")
              ? "Add an OpenAI-compatible endpoint (a local model server works), then verify it and set it as the default model."
              : "An owner needs to add a model provider before AI-assisted steps can run."}
          </Empty>
        )}
        {providers.data && providers.data.items.length > 0 && (
          <ul className="item-list">
            {providers.data.items.map((provider) => (
              <ProviderRow key={provider.id} provider={provider} onChanged={providersChanged} />
            ))}
          </ul>
        )}
      </Card>

      {can("owner") ? (
        <AddProvider kinds={kinds} onCreated={providersChanged} />
      ) : (
        <p className="muted small">Only an owner can add or delete model providers.</p>
      )}

      <RoleRoutingTable providers={providers.data?.items ?? []} policies={policies} />

      <Card title="Workspace model usage"
        actions={<button type="button" className="btn btn-small" onClick={usage.reload}>Refresh</button>}>
        {usage.loading && !usage.data && <Loading />}
        <ErrorNote error={usage.error} />
        {usage.data && (
          <>
            <UsageTable usage={usage.data} />
            <p className="muted small">{usage.data.cost_note}</p>
          </>
        )}
      </Card>
    </>
  );
}
