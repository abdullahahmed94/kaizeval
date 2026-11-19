import { useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useSession } from "../auth";
import { Badge, Card, Check, Empty, ErrorNote, Field, Loading, Notice, PageHeader, StatusBadge, TableWrap, When } from "../components/ui";
import { humanize, parseOptionalNumber } from "../format";
import { useAction, useLoad } from "../hooks";
import type { AccessLevel, AdaptersResponse, Dict } from "../types";

const LEVELS: AccessLevel[] = ["black_box", "read_only", "candidate_write"];
const LEVEL_LABELS: Record<AccessLevel, string> = {
  black_box: "Black box (invoke only)",
  read_only: "Read only (invoke and read configuration)",
  candidate_write: "Candidate write (patch an isolated candidate copy)",
};

type CloudField = { key: string; label: string; hint: string; optional?: boolean };

// Agents hosted on a cloud platform: the settings the platform needs, and what its credential is.
const CLOUD: Record<string, { fields: CloudField[]; credential: string; credentialHint: string }> = {
  aws_bedrock_agent: {
    fields: [
      { key: "region", label: "Region", hint: "The AWS Region of the agent, for example us-east-1." },
      { key: "agent_id", label: "Agent ID", hint: "The ten-character id shown on the agent page." },
      { key: "agent_alias_id", label: "Agent alias ID", hint: "The alias to test. TSTALIASID is the working draft." },
    ],
    credential: "AWS credential",
    credentialHint: "ACCESS_KEY_ID:SECRET_ACCESS_KEY (add :SESSION_TOKEN for temporary credentials). It needs permission for bedrock:InvokeAgent. Stored encrypted; never shown again.",
  },
  aws_agentcore: {
    fields: [
      { key: "agent_runtime_arn", label: "Agent runtime ARN", hint: "The ARN of the AgentCore runtime. The Region is read from it." },
      { key: "qualifier", label: "Endpoint (optional)", hint: "The runtime endpoint name. Default: DEFAULT.", optional: true },
      { key: "message_field", label: "Message field (optional)", hint: "The JSON field your agent reads the question from. Default: prompt.", optional: true },
      { key: "response_path", label: "Response path (optional)", hint: "Dotted path to the answer text in the reply of your agent. Empty tries the usual field names.", optional: true },
    ],
    credential: "AWS credential",
    credentialHint: "ACCESS_KEY_ID:SECRET_ACCESS_KEY (add :SESSION_TOKEN for temporary credentials). It needs permission for bedrock-agentcore:InvokeAgentRuntime. Stored encrypted; never shown again.",
  },
  salesforce_agentforce: {
    fields: [
      { key: "my_domain", label: "My Domain URL", hint: "For example https://yourcompany.my.salesforce.com" },
      { key: "agent_id", label: "Agent ID", hint: "The 18-character id at the end of the agent's URL in Setup (starts with 0Xx)." },
      { key: "client_id", label: "Consumer key", hint: "From the connected app that has the client credentials flow enabled." },
    ],
    credential: "Consumer secret",
    credentialHint: "The consumer secret of the same connected app. Stored encrypted; never shown again.",
  },
};

function RegisterAgent({ adapters }: { adapters: AdaptersResponse }) {
  const { api, can } = useSession();
  const navigate = useNavigate();
  const adapterNames = Object.keys(adapters.adapters);
  const [name, setName] = useState("");
  const [adapter, setAdapter] = useState(adapterNames[0] ?? "");
  const [url, setUrl] = useState("");
  const [style, setStyle] = useState("simple");
  const [responsePath, setResponsePath] = useState("");
  const [citationsPath, setCitationsPath] = useState("");
  const [credential, setCredential] = useState("");
  const [costPerCall, setCostPerCall] = useState("");
  const [accessLevel, setAccessLevel] = useState<AccessLevel>("black_box");
  const [allowPromote, setAllowPromote] = useState(false);
  const [cloudValues, setCloudValues] = useState<Record<string, string>>({});
  const [allowPublic, setAllowPublic] = useState(false);
  const action = useAction();
  const cloud = CLOUD[adapter];

  const info = adapters.adapters[adapter];
  const maxLevel = info ? LEVELS.indexOf(info.max_access_level) : 0;
  const owner = can("owner");
  const needsOwner = accessLevel === "candidate_write" || allowPromote;

  const chooseAdapter = (value: string) => {
    setAdapter(value);
    const limit = LEVELS.indexOf(adapters.adapters[value]?.max_access_level ?? "black_box");
    if (LEVELS.indexOf(accessLevel) > limit) setAccessLevel(LEVELS[limit]);
    if (limit < LEVELS.indexOf("candidate_write")) setAllowPromote(false);
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const config: Dict = {};
    const cost = parseOptionalNumber(costPerCall);
    if (cloud) {
      for (const field of cloud.fields) {
        const value = (cloudValues[field.key] ?? "").trim();
        if (value) config[field.key] = value;
      }
    } else if (adapter === "local_config") {
      config.base_url = url.trim();
    } else {
      config.url = url.trim();
      config.style = style;
      if (responsePath.trim()) config.response_path = responsePath.trim();
      if (citationsPath.trim()) config.citations_path = citationsPath.trim();
    }
    if (cost !== null) config.cost_per_call = cost;
    if (allowPublic && adapter !== "local_config") config.allow_public = true;
    void action.run(async () => {
      const created = await api.createAgent({
        name: name.trim(), adapter, config, credential: credential || null,
        access_level: accessLevel, allow_promote: allowPromote && accessLevel === "candidate_write",
      });
      navigate(`/agents/${created.id}`);
    });
  };

  return (
    <Card title="Register an agent">
      <form onSubmit={submit} className="form">
        <div className="form-grid">
          <Field label="Name">
            <input type="text" value={name} onChange={(event) => setName(event.target.value)} required maxLength={200} />
          </Field>
          <Field label="Adapter">
            <select value={adapter} onChange={(event) => chooseAdapter(event.target.value)}>
              {adapterNames.map((key) => (
                <option key={key} value={key}>{adapters.adapters[key].label}</option>
              ))}
            </select>
          </Field>
        </div>
        {info && (
          <Notice>
            <p>{info.description}</p>
            <p className="small">Highest access level this adapter supports: {humanize(info.max_access_level)}.</p>
          </Notice>
        )}

        {cloud ? (
          <div className="form-grid">
            {cloud.fields.map((field) => (
              <Field key={field.key} label={field.label} hint={field.hint}>
                <input type="text" value={cloudValues[field.key] ?? ""} required={!field.optional} maxLength={500}
                  onChange={(event) => setCloudValues({ ...cloudValues, [field.key]: event.target.value })} />
              </Field>
            ))}
            <Field label={cloud.credential} hint={cloud.credentialHint}>
              <input type="password" value={credential} onChange={(event) => setCredential(event.target.value)} autoComplete="off" />
            </Field>
            <Field label="Cost per call (optional)" hint="Your own estimate, used only to report target agent cost.">
              <input type="number" value={costPerCall} min={0} step="any" onChange={(event) => setCostPerCall(event.target.value)} />
            </Field>
          </div>
        ) : adapter === "local_config" ? (
          <div className="form-grid">
            <Field label="Base URL" hint="Root address of the agent, for example http://127.0.0.1:8101">
              <input type="url" value={url} onChange={(event) => setUrl(event.target.value)} required />
            </Field>
            <Field label="Admin credential" hint="Token for the agent's admin protocol. Stored encrypted; never shown again.">
              <input type="password" value={credential} onChange={(event) => setCredential(event.target.value)} autoComplete="off" />
            </Field>
            <Field label="Cost per call (optional)" hint="Your own estimate, used only to report target agent cost.">
              <input type="number" value={costPerCall} min={0} step="any" onChange={(event) => setCostPerCall(event.target.value)} />
            </Field>
          </div>
        ) : (
          <div className="form-grid">
            <Field label="URL" hint="The endpoint that receives each question.">
              <input type="url" value={url} onChange={(event) => setUrl(event.target.value)} required />
            </Field>
            <Field label="Request style"
              hint={style === "openai" ? "Sends an OpenAI-style chat completions body." : "Sends {message, session_id, history}."}>
              <select value={style} onChange={(event) => setStyle(event.target.value)}>
                <option value="simple">simple</option>
                <option value="openai">openai</option>
              </select>
            </Field>
            <Field label="Response path (optional)"
              hint={`Dotted path to the answer text. Default: ${style === "openai" ? "choices.0.message.content" : "answer"}.`}>
              <input type="text" value={responsePath} onChange={(event) => setResponsePath(event.target.value)} />
            </Field>
            <Field label="Citations path (optional)" hint="Dotted path to the cited sources. Default: citations.">
              <input type="text" value={citationsPath} onChange={(event) => setCitationsPath(event.target.value)} />
            </Field>
            <Field label="Credential (optional)" hint="Sent as a bearer token. Stored encrypted; never shown again.">
              <input type="password" value={credential} onChange={(event) => setCredential(event.target.value)} autoComplete="off" />
            </Field>
            <Field label="Cost per call (optional)" hint="Your own estimate, used only to report target agent cost.">
              <input type="number" value={costPerCall} min={0} step="any" onChange={(event) => setCostPerCall(event.target.value)} />
            </Field>
          </div>
        )}

        {adapter !== "local_config" && (
          <Check label="This agent is on a public address" checked={allowPublic} onChange={setAllowPublic}
            hint={cloud
              ? "Required for a hosted platform. Test questions are sent to it over the internet, and the workspace must be in connected mode."
              : "Tick it when the agent is not on your local network. The workspace must be in connected mode."} />
        )}
        {cloud && (
          <Notice tone="warn" title="Tests are real conversations with the deployed agent">
            <p>
              Every test case opens a session with this agent on the platform. If the agent can take actions
              (create a case, issue a refund, send an email), those actions really happen, and the platform bills
              the usage. Point Kaizeval at a sandbox or a test agent before you run a large test set.
            </p>
          </Notice>
        )}
        <Field label="Access level" hint={`Grants: ${(adapters.access_levels[accessLevel] ?? []).map(humanize).join(", ") || "—"}.`}>
          <select value={accessLevel} onChange={(event) => setAccessLevel(event.target.value as AccessLevel)}>
            {LEVELS.map((level, index) => (
              <option key={level} value={level} disabled={index > maxLevel}>
                {LEVEL_LABELS[level]}{index > maxLevel ? " — not supported by this adapter" : ""}
              </option>
            ))}
          </select>
        </Field>
        <Check label="Allow promotion to production through the connector" checked={allowPromote && accessLevel === "candidate_write"}
          disabled={accessLevel !== "candidate_write"} onChange={setAllowPromote}
          hint="Only with candidate write access. Promotion still needs an explicit owner approval for each candidate." />
        {needsOwner && !owner && (
          <Notice tone="warn">Candidate write access and promotion can only be granted by an owner. Your role is editor.</Notice>
        )}
        <ErrorNote error={action.error} />
        <div className="button-row">
          <button type="submit" className="btn btn-primary" disabled={action.busy || (needsOwner && !owner)}>
            {action.busy ? "Registering…" : "Register agent"}
          </button>
        </div>
      </form>
    </Card>
  );
}

export function AgentsPage() {
  const { api, can } = useSession();
  const agents = useLoad(() => api.agents(), [api]);
  const adapters = useLoad(() => api.adapters(), [api]);

  return (
    <>
      <PageHeader title="Agents" subtitle="The agents under test. Kaizeval only does what the connector supports and what you granted." />
      <Card title="Registered agents">
        {agents.loading && !agents.data && <Loading />}
        <ErrorNote error={agents.error} />
        {agents.data && agents.data.items.length === 0 && (
          <Empty title="No agent registered yet.">
            {can("editor") ? "Register the agent you want to evaluate using the form below, then verify its connection."
              : "An editor needs to register an agent before it can be evaluated."}
          </Empty>
        )}
        {agents.data && agents.data.items.length > 0 && (
          <TableWrap>
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Adapter</th>
                  <th scope="col">Access level</th>
                  <th scope="col">Promotion</th>
                  <th scope="col">Connection</th>
                  <th scope="col">Capabilities</th>
                </tr>
              </thead>
              <tbody>
                {agents.data.items.map((agent) => (
                  <tr key={agent.id}>
                    <th scope="row"><Link to={`/agents/${agent.id}`}>{agent.name}</Link></th>
                    <td>{adapters.data?.adapters[agent.adapter]?.label ?? agent.adapter}</td>
                    <td>{humanize(agent.access_level)}</td>
                    <td>{agent.allow_promote ? "granted" : "not granted"}</td>
                    <td>
                      {agent.verified ? (
                        <>
                          <StatusBadge status="verified" /> <span className="muted small"><When value={agent.verified_at} /></span>
                        </>
                      ) : agent.last_verification && !agent.last_verification.ok ? (
                        <Badge tone="bad">Verification failed</Badge>
                      ) : (
                        <Badge tone="warn">Not verified</Badge>
                      )}
                    </td>
                    <td>{agent.capabilities.length ? agent.capabilities.map(humanize).join(", ") : <span className="muted">none yet</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Card>
      <ErrorNote error={adapters.error} />
      {can("editor") ? (
        adapters.data && <RegisterAgent adapters={adapters.data} />
      ) : (
        <p className="muted small">Only editors and owners can register agents.</p>
      )}
    </>
  );
}
