import { useState, type FormEvent } from "react";

import { useSession } from "../auth";
import { Card, Empty, ErrorNote, Field, Loading, Notice, PageHeader, Pager, TableWrap, When } from "../components/ui";
import { humanize } from "../format";
import { useLoad } from "../hooks";

const PAGE = 50;

export function AuditPage() {
  const { api, can, role, userLabel } = useSession();
  const allowed = can("reviewer");
  const [draft, setDraft] = useState("");
  const [action, setAction] = useState("");
  const [offset, setOffset] = useState(0);
  const events = useLoad(
    async () => (allowed ? api.audit({ action: action || undefined, limit: PAGE, offset }) : null),
    [api, allowed, action, offset],
  );
  const data = events.data;

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setAction(draft.trim());
    setOffset(0);
  };

  if (!allowed) {
    return (
      <>
        <PageHeader title="Audit log" />
        <Notice tone="warn">The audit log is available to reviewers, editors and owners. Your role is {role}.</Notice>
      </>
    );
  }

  return (
    <>
      <PageHeader title="Audit log" subtitle="Who did what in this workspace, newest first." />
      <Card title="Events" actions={<button type="button" className="btn btn-small" onClick={events.reload}>Refresh</button>}>
        <form onSubmit={submit} className="form form-inline">
          <Field label="Filter by action prefix" hint='For example "holdout." or "candidate.promoted".'>
            <input type="text" value={draft} onChange={(event) => setDraft(event.target.value)} />
          </Field>
          <button type="submit" className="btn">Apply filter</button>
          {action && (
            <button type="button" className="btn" onClick={() => { setDraft(""); setAction(""); setOffset(0); }}>
              Clear
            </button>
          )}
        </form>
        {events.loading && !data && <Loading />}
        <ErrorNote error={events.error} />
        {data && data.items.length === 0 && (
          <Empty title={action ? `No event has an action starting with "${action}".` : "No event has been recorded yet."} />
        )}
        {data && data.items.length > 0 && (
          <TableWrap>
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Time</th>
                  <th scope="col">User</th>
                  <th scope="col">Action</th>
                  <th scope="col">Subject</th>
                  <th scope="col">Detail</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((event) => (
                  <tr key={event.id}>
                    <td><When value={event.created_at} /></td>
                    <td>{event.user_id ? userLabel(event.user_id) : "system"}</td>
                    <th scope="row"><code>{event.action}</code></th>
                    <td>
                      {event.subject_type ? humanize(event.subject_type) : "—"}
                      {event.subject_id && <span className="muted small"> <code title={event.subject_id}>{event.subject_id.slice(0, 8)}</code></span>}
                    </td>
                    <td className="cell-wide">
                      {Object.keys(event.detail ?? {}).length > 0 ? <code className="wrap">{JSON.stringify(event.detail)}</code> : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
        {data && <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />}
      </Card>
    </>
  );
}
