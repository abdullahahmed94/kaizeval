import { useEffect, useState, type FormEvent } from "react";

import { system } from "../api";
import { useSession } from "../auth";
import {
  Badge, Card, Check, ConfirmDialog, Empty, ErrorNote, Field, Loading, Notice, PageHeader, TableWrap, When,
} from "../components/ui";
import { useAction, useLoad } from "../hooks";
import type { Member, Role, Workspace } from "../types";

const ROLES: Role[] = ["owner", "editor", "reviewer", "viewer"];

function modeLabel(workspace: Workspace): string {
  return workspace.effective_offline ? "Offline (local only)" : "Connected";
}

function CreateWorkspace() {
  const session = useSession();
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"offline" | "connected">("offline");
  const action = useAction();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void action.run(async () => {
      const created = await system.createWorkspace(name.trim(), mode);
      setName("");
      await session.refresh();
      session.selectWorkspace(created.id);
    });
  };

  return (
    <Card title="Create a workspace">
      <form onSubmit={submit} className="form form-inline">
        <Field label="Name">
          <input type="text" value={name} onChange={(event) => setName(event.target.value)} required maxLength={200} />
        </Field>
        <Field label="Mode" hint="Offline reaches only local or allowlisted endpoints. Connected allows owner-approved public endpoints.">
          <select value={mode} onChange={(event) => setMode(event.target.value as "offline" | "connected")}>
            <option value="offline">Offline (local only)</option>
            <option value="connected">Connected</option>
          </select>
        </Field>
        <button type="submit" className="btn btn-primary" disabled={action.busy || !name.trim()}>
          {action.busy ? "Creating…" : "Create workspace"}
        </button>
      </form>
      <ErrorNote error={action.error} />
    </Card>
  );
}

function WorkspaceSettings({ workspace }: { workspace: Workspace }) {
  const session = useSession();
  const owner = session.can("owner");
  const [name, setName] = useState(workspace.name);
  const [mode, setMode] = useState(workspace.mode);
  const [autonomous, setAutonomous] = useState(Boolean(workspace.settings?.allow_autonomous_optimization));
  const [saved, setSaved] = useState(false);
  const action = useAction();

  useEffect(() => {
    setName(workspace.name);
    setMode(workspace.mode);
    setAutonomous(Boolean(workspace.settings?.allow_autonomous_optimization));
  }, [workspace]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setSaved(false);
    void action.run(async () => {
      await session.api.updateWorkspace({
        name: name.trim(), mode, settings: { allow_autonomous_optimization: autonomous },
      });
      await session.refresh();
      setSaved(true);
    });
  };

  return (
    <Card title={`Settings for "${workspace.name}"`}>
      {!owner && <Notice>Only an owner can change workspace settings. Your role is {session.role}.</Notice>}
      <form onSubmit={submit} className="form">
        <div className="form-grid">
          <Field label="Name">
            <input type="text" value={name} onChange={(event) => setName(event.target.value)} disabled={!owner} required maxLength={200} />
          </Field>
          <Field label="Mode"
            hint={workspace.instance_offline
              ? "This instance runs in strict offline mode; a workspace cannot override it."
              : "Connected lets an owner approve public model or agent endpoints."}>
            <select value={mode} onChange={(event) => setMode(event.target.value as "offline" | "connected")} disabled={!owner}>
              <option value="offline">Offline (local only)</option>
              <option value="connected">Connected</option>
            </select>
          </Field>
        </div>
        <Check label="Allow autonomous optimization" checked={autonomous} onChange={setAutonomous} disabled={!owner}
          hint="When off, every proposed change to a candidate needs a reviewer's approval (human-guided mode). Nothing is ever promoted to production automatically in either mode." />
        <ErrorNote error={action.error} />
        {saved && <p className="saved" role="status">Settings saved.</p>}
        {owner && (
          <div className="button-row">
            <button type="submit" className="btn btn-primary" disabled={action.busy || !name.trim()}>
              {action.busy ? "Saving…" : "Save settings"}
            </button>
          </div>
        )}
      </form>
    </Card>
  );
}

function Members() {
  const session = useSession();
  const { api } = session;
  const owner = session.can("owner");
  const members = useLoad(() => api.members(), [api]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const add = useAction();
  const change = useAction();
  const [removing, setRemoving] = useState<Member | null>(null);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void add.run(async () => {
      await api.setMember(email.trim(), role);
      setEmail("");
      members.reload();
    });
  };

  const setMemberRole = (member: Member, next: Role) =>
    void change.run(async () => {
      await api.setMember(member.email, next);
      members.reload();
      if (member.user_id === session.user?.id) await session.refresh();
    });

  const remove = () => {
    if (!removing) return;
    const target = removing;
    void change.run(async () => {
      try {
        await api.removeMember(target.user_id);
      } finally {
        setRemoving(null);
      }
      members.reload();
      if (target.user_id === session.user?.id) await session.refresh();
    });
  };

  return (
    <Card title="Members">
      {members.loading && !members.data && <Loading />}
      <ErrorNote error={members.error} />
      {members.data && (
        <TableWrap>
          <table className="data">
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Email</th>
                <th scope="col">Role</th>
                {owner && <th scope="col">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {members.data.items.map((member) => (
                <tr key={member.user_id}>
                  <td>{member.name || "—"}</td>
                  <td>{member.email}</td>
                  <td>
                    {owner ? (
                      <select aria-label={`Role of ${member.email}`} value={member.role} disabled={change.busy}
                        onChange={(event) => setMemberRole(member, event.target.value as Role)}>
                        {ROLES.map((item) => (
                          <option key={item} value={item}>{item}</option>
                        ))}
                      </select>
                    ) : (
                      member.role
                    )}
                  </td>
                  {owner && (
                    <td>
                      <button type="button" className="btn btn-small btn-danger" disabled={change.busy} onClick={() => setRemoving(member)}>
                        Remove
                      </button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </TableWrap>
      )}
      <ErrorNote error={change.error} />
      {owner ? (
        <form onSubmit={submit} className="form form-inline">
          <Field label="Add a member by email" hint="The person must already have an account on this instance.">
            <input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required />
          </Field>
          <Field label="Role">
            <select value={role} onChange={(event) => setRole(event.target.value as Role)}>
              {ROLES.map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </select>
          </Field>
          <button type="submit" className="btn" disabled={add.busy || !email.trim()}>
            {add.busy ? "Adding…" : "Add member"}
          </button>
        </form>
      ) : (
        <p className="muted small">Only an owner can add, change or remove members.</p>
      )}
      <ErrorNote error={add.error} />
      <ConfirmDialog open={removing !== null} title="Remove member" confirmLabel="Remove" danger busy={change.busy}
        onConfirm={remove} onCancel={() => setRemoving(null)}>
        <p>
          Remove <strong>{removing?.email}</strong> from this workspace? They will lose access to its data immediately.
        </p>
      </ConfirmDialog>
    </Card>
  );
}

export function WorkspacesPage() {
  const session = useSession();
  const { workspaces, workspace } = session;

  return (
    <>
      <PageHeader title="Workspaces and members" subtitle="A workspace isolates agents, documents, tests and results." />
      <Card title="My workspaces">
        {workspaces.length === 0 ? (
          <Empty title="You are not a member of any workspace yet.">
            Create one below, or ask an owner of an existing workspace to add your account.
          </Empty>
        ) : (
          <TableWrap>
            <table className="data">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Mode</th>
                  <th scope="col">My role</th>
                  <th scope="col">Created</th>
                  <th scope="col">Actions</th>
                </tr>
              </thead>
              <tbody>
                {workspaces.map((item) => (
                  <tr key={item.id}>
                    <th scope="row">{item.name}</th>
                    <td>
                      <Badge tone={item.effective_offline ? "ok" : "warn"}>{modeLabel(item)}</Badge>
                    </td>
                    <td>{item.role}</td>
                    <td><When value={item.created_at} /></td>
                    <td>
                      {item.id === workspace?.id ? (
                        <Badge tone="info">Current</Badge>
                      ) : (
                        <button type="button" className="btn btn-small" onClick={() => session.selectWorkspace(item.id)}>
                          Switch to this workspace
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </TableWrap>
        )}
      </Card>
      <CreateWorkspace />
      {workspace && <WorkspaceSettings key={`settings-${workspace.id}`} workspace={workspace} />}
      {workspace && <Members key={`members-${workspace.id}`} />}
    </>
  );
}
