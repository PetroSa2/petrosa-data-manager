import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

export interface EndpointState<T> {
  data: T | null;
  error: ApiError | null;
  loading: boolean;
  // Number of successful fetches so far; useful for distinguishing "first
  // load, no data yet" from "refresh polling, last value still visible".
  generation: number;
  refresh: () => void;
}

// Tiny hook: call `fetcher` on mount and every `intervalMs` after. Keeps the
// last successful payload visible across refresh failures so a transient
// 5xx doesn't blank a populated pane. Cancels stale resolutions on unmount.
export function useEndpoint<T>(
  fetcher: () => Promise<T>,
  intervalMs = 15_000,
): EndpointState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [generation, setGeneration] = useState<number>(0);
  const aliveRef = useRef(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const run = useCallback(async () => {
    setLoading(true);
    try {
      const next = await fetcherRef.current();
      if (!aliveRef.current) return;
      setData(next);
      setError(null);
      setGeneration((g) => g + 1);
    } catch (e) {
      if (!aliveRef.current) return;
      if (e instanceof ApiError) setError(e);
      else setError(new ApiError(0, null, (e as Error).message));
    } finally {
      if (aliveRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    void run();
    const id = window.setInterval(() => {
      void run();
    }, intervalMs);
    return () => {
      aliveRef.current = false;
      window.clearInterval(id);
    };
  }, [run, intervalMs]);

  return { data, error, loading, generation, refresh: run };
}
