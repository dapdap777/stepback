import { useCallback, useEffect, useRef, useState } from "react";
import { api, type StepDetail } from "./client";

export interface AsyncState<T> {
  data: T | undefined;
  error: Error | undefined;
  loading: boolean;
  reload: () => void;
}

/** Run `load` whenever `key` changes. Stale responses (from an older key) are ignored. */
export function useAsync<T>(key: string | null, load: () => Promise<T>): AsyncState<T> {
  const [state, setState] = useState<{ data?: T; error?: Error; loading: boolean }>({
    loading: key !== null,
  });
  const [nonce, setNonce] = useState(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  useEffect(() => {
    if (key === null) {
      setState({ loading: false });
      return;
    }
    let cancelled = false;
    setState((s) => ({ data: s.data, loading: true }));
    loadRef
      .current()
      .then((data) => !cancelled && setState({ data, loading: false }))
      .catch((error: Error) => !cancelled && setState({ error, loading: false }));
    return () => {
      cancelled = true;
    };
  }, [key, nonce]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);
  return { data: state.data, error: state.error, loading: state.loading, reload };
}

// Step bodies are immutable once recorded, so they can be cached for the whole session.
const stepCache = new Map<string, Promise<StepDetail>>();

export function fetchStep(stepId: string): Promise<StepDetail> {
  let cached = stepCache.get(stepId);
  if (!cached) {
    cached = api.getStep(stepId);
    cached.catch(() => stepCache.delete(stepId));
    stepCache.set(stepId, cached);
  }
  return cached;
}

export function useStep(stepId: string | null): AsyncState<StepDetail> {
  return useAsync(stepId, () => fetchStep(stepId as string));
}
