import { useEffect, useRef, useState } from 'react';
import type { ServerEvent } from './types';

/**
 * Subscribes to the server's event stream. Exploration and healing are
 * long-running background work — the console watches rather than polls.
 */
export function useEvents(onEvent: (event: ServerEvent) => void) {
  const [connected, setConnected] = useState(false);
  const handler = useRef(onEvent);
  handler.current = onEvent;

  useEffect(() => {
    const source = new EventSource('/api/events');
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.onmessage = (message) => {
      try {
        handler.current(JSON.parse(message.data) as ServerEvent);
      } catch {
        /* keep-alive comments and malformed frames are not fatal */
      }
    };
    return () => source.close();
  }, []);

  return connected;
}

/** Fetch-on-mount with an explicit refresh, so views can react to events. */
export function useResource<T>(load: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const loader = useRef(load);
  loader.current = load;

  useEffect(() => {
    let cancelled = false;
    loader
      .current()
      .then((value) => !cancelled && (setData(value), setError(null)))
      .catch((exc: Error) => !cancelled && setError(exc.message));
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, ...deps]);

  return { data, error, refresh: () => setNonce((n) => n + 1) };
}
