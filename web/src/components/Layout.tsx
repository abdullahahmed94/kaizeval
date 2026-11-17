import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useSession } from "../auth";
import type { Role } from "../types";
import { Badge } from "./ui";

interface NavItem {
  to: string;
  label: string;
  end?: boolean;
  minRole?: Role;
}

const NAV: { heading: string; items: NavItem[] }[] = [
  { heading: "Workspace", items: [{ to: "/", label: "Overview", end: true }] },
  {
    heading: "Set up",
    items: [
      { to: "/models", label: "Models" },
      { to: "/agents", label: "Agents" },
      { to: "/knowledge", label: "Knowledge" },
      { to: "/specifications", label: "Specification" },
      { to: "/datasets", label: "Datasets" },
    ],
  },
  {
    heading: "Evaluate and improve",
    items: [
      { to: "/evaluations", label: "Evaluations" },
      { to: "/recommendations", label: "Recommendations" },
      { to: "/optimizations", label: "Optimization" },
    ],
  },
  {
    heading: "Administration",
    items: [
      { to: "/audit", label: "Audit log", minRole: "reviewer" },
      { to: "/workspaces", label: "Workspaces and members" },
    ],
  },
];

export function ModeBadge() {
  const { workspace } = useSession();
  if (!workspace) return null;
  return workspace.effective_offline ? (
    <Badge tone="ok" title="Only local or allowlisted model and agent endpoints can be reached.">
      Offline (local only)
    </Badge>
  ) : (
    <Badge tone="warn" title="Owner-approved public endpoints can be reached.">
      Connected
    </Badge>
  );
}

export function Layout() {
  const { user, workspaces, workspace, role, can, selectWorkspace, signOut } = useSession();
  const [menuOpen, setMenuOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => setMenuOpen(false), [location.pathname]);

  return (
    <div className="shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <div className="sidebar-top">
          <NavLink to="/" className="brand" end>
            Kaizeval
          </NavLink>
          <ModeBadge />
          <button type="button" className="btn btn-small menu-toggle" aria-expanded={menuOpen} aria-controls="site-menu"
            onClick={() => setMenuOpen((open) => !open)}>
            Menu
          </button>
        </div>
        <div id="site-menu" className={menuOpen ? "sidebar-body open" : "sidebar-body"}>
          <label className="field workspace-switch">
            <span className="field-label">Workspace</span>
            <select
              value={workspace?.id ?? ""}
              disabled={workspaces.length === 0}
              onChange={(event) => {
                selectWorkspace(event.target.value);
                navigate("/");
              }}
            >
              {workspaces.length === 0 && <option value="">No workspace yet</option>}
              {workspaces.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
            {role && <span className="field-hint">Your role: {role}</span>}
          </label>
          <nav aria-label="Main">
            {NAV.map((group) => (
              <div key={group.heading} className="nav-group">
                <p className="nav-heading">{group.heading}</p>
                <ul>
                  {group.items
                    .filter((item) => !item.minRole || can(item.minRole))
                    .map((item) => (
                      <li key={item.to}>
                        <NavLink to={item.to} end={item.end} className={({ isActive }) => (isActive ? "nav-link active" : "nav-link")}>
                          {item.label}
                        </NavLink>
                      </li>
                    ))}
                </ul>
              </div>
            ))}
          </nav>
          <div className="sidebar-user">
            <p className="user-name" title={user?.email}>
              {user?.name || user?.email}
            </p>
            {user?.name && <p className="muted small">{user.email}</p>}
            <button type="button" className="btn btn-small" onClick={signOut}>
              Sign out
            </button>
          </div>
        </div>
      </aside>
      <main id="main" className="main" tabIndex={-1}>
        {/* Remount every page when the workspace changes so no data from another workspace lingers. */}
        <Outlet key={workspace?.id ?? "none"} />
      </main>
    </div>
  );
}
