import type { Approval, Benchmark, Connector, DiffResult, Run } from './types';

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'content-type': 'application/json' },
    ...init,
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(`${response.status} ${path}: ${detail.slice(0, 200)}`);
  }
  return response.json() as Promise<T>;
}

export const api = {
  connectors: () => json<Connector[]>('/api/connectors'),
  connector: (id: string) => json<Connector>(`/api/connectors/${id}`),
  openapi: (id: string) => json<unknown>(`/api/connectors/${id}/openapi`),
  skill: (id: string) => json<{ skill_md: string }>(`/api/connectors/${id}/skill`),
  diff: (id: string, base: string, head: string) =>
    json<DiffResult>(`/api/connectors/${id}/diff?base=${base}&head=${head}`),

  runs: (connectorId?: string) =>
    json<Run[]>(`/api/runs${connectorId ? `?connector_id=${connectorId}` : ''}`),
  run: (id: string) => json<Run>(`/api/runs/${id}`),

  benchmark: (id: string) => json<Benchmark>(`/api/benchmarks/${id}`),

  approvals: () => json<Approval[]>('/api/approvals'),
  decideApproval: (id: string, decision: 'approve' | 'deny') =>
    json<Approval>(`/api/approvals/${id}/${decision}`, { method: 'POST' }),

  /** Only start_url and goal are required; the rest is derived from the URL. */
  explore: (body: {
    start_url: string;
    goal: string;
    connector_id?: string;
    operation?: string;
    allowed_hosts?: string[];
    username?: string;
    password?: string;
  }) =>
    json<{
      accepted: boolean;
      connector_id: string;
      operation: string;
      allowed_hosts: string[];
      credentials_stored: boolean;
    }>('/api/explore', { method: 'POST', body: JSON.stringify(body) }),

  answerApproval: (id: string, value: string) =>
    json<{ answered: boolean }>(`/api/approvals/${id}/answer`, {
      method: 'POST',
      body: JSON.stringify({ value }),
    }),

  heal: (id: string) => json<{ accepted: boolean }>(`/api/heal/${id}`, { method: 'POST' }),

  invoke: (connectorId: string, operation: string, payload: Record<string, unknown>) =>
    json<Record<string, unknown>>(`/connectors/${connectorId}/${operation}`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
};

export const artifactUrl = (runId: string, screenshotPath: string) => {
  const name = screenshotPath.split('/').pop() ?? '';
  return `/api/artifacts/${runId}/${name}`;
};
