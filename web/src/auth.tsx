import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { ApiError, getToken, setToken, setUnauthorizedHandler, system, workspaceApi, type WorkspaceApi } from "./api";
import type { Member, Role, User, Workspace } from "./types";

const WORKSPACE_KEY = "kaizeval.workspace";
const ROLE_RANK: Record<Role, number> = { viewer: 0, reviewer: 1, editor: 2, owner: 3 };

interface SessionValue {
  /** True until the first /auth/me answer (or its failure) is known. */
  loading: boolean;
  error: unknown;
  user: User | null;
  workspaces: Workspace[];
  workspace: Workspace | null;
  role: Role | null;
  /** API bound to the current workspace. Only use it on pages rendered inside a workspace. */
  api: WorkspaceApi;
  /** Whether the signed-in user holds at least ``minimum`` in the current workspace. */
  can: (minimum: Role) => boolean;
  signIn: (token: string) => Promise<void>;
  /** Sign out on the user's request. */
  signOut: () => void;
  /** True after a deliberate sign-out, so the next sign-in starts at the overview instead of the last page. */
  signedOut: boolean;
  refresh: () => Promise<void>;
  selectWorkspace: (id: string) => void;
  /** Display name for a user id found in a record (members of the current workspace). */
  userLabel: (id: string | null | undefined) => string;
}

const SessionContext = createContext<SessionValue | null>(null);

function storedWorkspace(): string | null {
  try {
    return window.localStorage.getItem(WORKSPACE_KEY);
  } catch {
    return null;
  }
}

export function SessionProvider({ children }: { children: ReactNode }) {
  const [loading, setLoading] = useState<boolean>(() => getToken() !== null);
  const [error, setError] = useState<unknown>(null);
  const [user, setUser] = useState<User | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [workspaceId, setWorkspaceId] = useState<string | null>(storedWorkspace);
  const [members, setMembers] = useState<Member[]>([]);
  const [signedOut, setSignedOut] = useState(false);

  const refresh = useCallback(async () => {
    if (!getToken()) {
      setUser(null);
      setWorkspaces([]);
      setLoading(false);
      return;
    }
    try {
      const me = await system.me();
      setUser(me.user);
      setWorkspaces(me.workspaces);
      setError(null);
    } catch (reason) {
      // An expired session simply returns to the login page; other failures are shown there.
      setError(reason instanceof ApiError && reason.status === 401 ? null : reason);
    } finally {
      setLoading(false);
    }
  }, []);

  const clearSession = useCallback(() => {
    setToken(null);
    setUser(null);
    setWorkspaces([]);
    setMembers([]);
  }, []);

  const signOut = useCallback(() => {
    setSignedOut(true);
    clearSession();
  }, [clearSession]);

  useEffect(() => {
    // An expired or rejected token ends the session but keeps the current page as the return target.
    setUnauthorizedHandler(clearSession);
    return () => setUnauthorizedHandler(null);
  }, [clearSession]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const signIn = useCallback(
    async (token: string) => {
      setToken(token);
      setSignedOut(false);
      setLoading(true);
      await refresh();
    },
    [refresh],
  );

  const workspace = useMemo(
    () => workspaces.find((item) => item.id === workspaceId) ?? workspaces[0] ?? null,
    [workspaces, workspaceId],
  );

  const selectWorkspace = useCallback((id: string) => {
    setWorkspaceId(id);
    try {
      window.localStorage.setItem(WORKSPACE_KEY, id);
    } catch {
      // The selection then only lasts for this page load.
    }
  }, []);

  const currentId = workspace?.id ?? "";
  const api = useMemo(() => workspaceApi(currentId), [currentId]);

  useEffect(() => {
    let active = true;
    setMembers([]);
    if (currentId && user) {
      api.members().then(
        (result) => {
          if (active) setMembers(result.items);
        },
        () => undefined,
      );
    }
    return () => {
      active = false;
    };
  }, [api, currentId, user]);

  const value = useMemo<SessionValue>(() => {
    const role = workspace?.role ?? null;
    const names = new Map(members.map((member) => [member.user_id, member.name || member.email]));
    return {
      loading,
      error,
      user,
      workspaces,
      workspace,
      role,
      api,
      can: (minimum) => role !== null && ROLE_RANK[role] >= ROLE_RANK[minimum],
      signIn,
      signOut,
      signedOut,
      refresh,
      selectWorkspace,
      userLabel: (id) => {
        if (!id) return "—";
        if (user && id === user.id) return user.name || user.email;
        return names.get(id) ?? `user ${id.slice(0, 8)}`;
      },
    };
  }, [loading, error, user, workspaces, workspace, api, members, signIn, signOut, signedOut, refresh, selectWorkspace]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (!value) throw new Error("useSession must be used inside SessionProvider");
  return value;
}
