import { useCallback, useEffect, useMemo, useRef, useState, type DependencyList } from "react";

import { useSession } from "./auth";
import type { Agent, Dataset } from "./types";

export interface LoadState<T> {
  data: T | undefined;
  error: unknown;
  loading: boolean;
  /** Fetch again. Existing data stays on screen until the new result arrives. */
  reload: () => void;
}

/** Load data when the component mounts and whenever ``deps`` change. */
export function useLoad<T>(loader: () => Promise<T>, deps: DependencyList): LoadState<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setData(undefined), deps);

  useEffect(() => {
    let active = true;
    setLoading(true);
    loaderRef.current().then(
      (result) => {
        if (!active) return;
        setData(result);
        setError(null);
        setLoading(false);
      },
      (reason: unknown) => {
        if (!active) return;
        setError(reason);
        setLoading(false);
      },
    );
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  const reload = useCallback(() => setTick((value) => value + 1), []);
  return { data, error, loading, reload };
}

export interface ActionState {
  busy: boolean;
  error: unknown;
  /** Run an async action, tracking its busy and error state. Resolves to false when it failed. */
  run: (action: () => Promise<void>) => Promise<boolean>;
  clear: () => void;
}

/** Tracks one user-triggered request so its error can be shown next to the control that caused it. */
export function useAction(): ActionState {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      return true;
    } catch (reason) {
      if (mounted.current) setError(reason);
      return false;
    } finally {
      if (mounted.current) setBusy(false);
    }
  }, []);
  const clear = useCallback(() => setError(null), []);
  return { busy, error, run, clear };
}

/** Agent and dataset names for the current workspace, used to label ids in tables. */
export function useNames(): {
  agents: Agent[];
  datasets: Dataset[];
  agentName: (id: string | null | undefined) => string;
  datasetName: (id: string | null | undefined) => string;
  loading: boolean;
  error: unknown;
  reload: () => void;
} {
  const { api } = useSession();
  const state = useLoad(
    async () => {
      const [agents, datasets] = await Promise.all([api.agents(), api.datasets({ limit: 200 })]);
      return { agents: agents.items, datasets: datasets.items };
    },
    [api],
  );
  return useMemo(() => {
    const agents = state.data?.agents ?? [];
    const datasets = state.data?.datasets ?? [];
    const agentMap = new Map(agents.map((agent) => [agent.id, agent.name]));
    const datasetMap = new Map(datasets.map((dataset) => [dataset.id, dataset.name]));
    return {
      agents,
      datasets,
      agentName: (id) => (id ? agentMap.get(id) ?? id.slice(0, 8) : "—"),
      datasetName: (id) => (id ? datasetMap.get(id) ?? id.slice(0, 8) : "—"),
      loading: state.loading,
      error: state.error,
      reload: state.reload,
    };
  }, [state.data, state.loading, state.error, state.reload]);
}
