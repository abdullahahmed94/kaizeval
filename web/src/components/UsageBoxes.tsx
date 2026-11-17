import { formatCost, formatInt, formatMs, humanize } from "../format";
import type { AgentUsage, Usage } from "../types";
import { Badge, TableWrap } from "./ui";

export function UsageTable({ usage }: { usage: Usage }) {
  const roles = Object.entries(usage.by_role ?? {});
  if (roles.length === 0) return <p className="muted">No model call has been recorded.</p>;
  return (
    <TableWrap>
      <table className="data">
        <thead>
          <tr>
            <th scope="col">Role</th>
            <th scope="col" className="num">Calls</th>
            <th scope="col" className="num">Prompt tokens</th>
            <th scope="col" className="num">Completion tokens</th>
            <th scope="col" className="num">Latency (total)</th>
            <th scope="col" className="num">Estimated cost</th>
            <th scope="col">Provider</th>
          </tr>
        </thead>
        <tbody>
          {roles.map(([role, row]) => (
            <tr key={role}>
              <th scope="row">{humanize(role)}</th>
              <td className="num">{formatInt(row.calls)}</td>
              <td className="num">{formatInt(row.prompt_tokens)}</td>
              <td className="num">{formatInt(row.completion_tokens)}</td>
              <td className="num">{formatMs(row.latency_ms)}</td>
              <td className="num">{formatCost(row.estimated_cost)}</td>
              <td>{row.provider_kind === "fake" ? <Badge tone="synthetic">fake</Badge> : row.provider_kind ?? "—"}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row">Total</th>
            <td className="num">{formatInt(usage.calls)}</td>
            <td className="num">{formatInt(usage.prompt_tokens)}</td>
            <td className="num">{formatInt(usage.completion_tokens)}</td>
            <td className="num">{formatMs(usage.latency_ms)}</td>
            <td className="num">{formatCost(usage.estimated_cost)}</td>
            <td />
          </tr>
        </tfoot>
      </table>
    </TableWrap>
  );
}

/** Platform model usage and target-agent cost are different things and are always shown apart. */
export function UsageBoxes({ models, agent }: { models: Usage | null | undefined; agent: AgentUsage | null | undefined }) {
  return (
    <div className="two-col">
      <section className="box" aria-label="Platform model usage">
        <h3>Platform model usage</h3>
        <p className="muted small">Models that Kaizeval itself calls to generate tests, judge answers and propose changes.</p>
        {models ? (
          <>
            <dl className="kv">
              <dt>Calls</dt>
              <dd>{formatInt(models.calls)}</dd>
              <dt>Prompt tokens</dt>
              <dd>{formatInt(models.prompt_tokens)}</dd>
              <dt>Completion tokens</dt>
              <dd>{formatInt(models.completion_tokens)}</dd>
              <dt>Latency (total)</dt>
              <dd>{formatMs(models.latency_ms)}</dd>
              <dt>Estimated cost</dt>
              <dd>{formatCost(models.estimated_cost)}</dd>
            </dl>
            <p className="muted small">{models.cost_note}</p>
          </>
        ) : (
          <p className="muted">Not reported.</p>
        )}
      </section>
      <section className="box" aria-label="Target agent cost">
        <h3>Target agent cost</h3>
        <p className="muted small">Calls made to the agent under test. Reported separately from platform models.</p>
        {agent ? (
          <>
            <dl className="kv">
              <dt>Agent calls</dt>
              <dd>{formatInt(agent.calls)}</dd>
              <dt>Estimated cost</dt>
              <dd>{formatCost(agent.estimated_cost)}</dd>
            </dl>
            <p className="muted small">{agent.cost_note}</p>
          </>
        ) : (
          <p className="muted">Not reported.</p>
        )}
      </section>
    </div>
  );
}
