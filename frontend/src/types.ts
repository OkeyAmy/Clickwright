/** Mirrors app/connectors/models.py. Keep the two in step. */

export type Action = 'navigate' | 'click' | 'type' | 'select' | 'wait' | 'assert';
export type RunMode = 'explore' | 'replay' | 'heal' | 'canary';

export interface Selector {
  primary: string;
  fallbacks: string[];
  accessible_name: string | null;
  text: string | null;
}

export interface Step {
  index: number;
  action: Action;
  value: string | null;
  url: string | null;
  selector: Selector | null;
  reason: string | null;
  ms: number;
  screenshot: string | null;
  status: 'ok' | 'failed';
}

export interface InputField {
  name: string;
  type: 'string' | 'number' | 'boolean';
  required: boolean;
  description: string;
  example: string | null;
}

export interface ConnectorVersion {
  version: string;
  created_at: string;
  status: 'active' | 'superseded' | 'quarantined';
  healed_from: string | null;
  heal_reason: string | null;
  steps: unknown[];
  inputs: InputField[];
  source_run_id: string | null;
}

export interface Connector {
  id: string;
  portal: string;
  operation: string;
  base_url: string;
  method: 'POST' | 'GET';
  owner: string;
  requires_approval: boolean;
  versions: ConnectorVersion[];
  path: string;
  active_version: string | null;
}

export interface PolicyEvent {
  kind: 'injection' | 'safety' | 'redaction';
  at_step: number | null;
  detail: string;
  action_taken: 'flagged' | 'blocked' | 'held' | 'redacted';
  at: string;
}

export interface Run {
  id: string;
  connector_id: string;
  mode: RunMode;
  version: string | null;
  started_at: string;
  duration_ms: number;
  model_cost_usd: number;
  status: 'ok' | 'failed' | 'held_for_approval';
  failed_step: number | null;
  error: string | null;
  result: Record<string, unknown>;
  policy_events: PolicyEvent[];
  steps: Step[];
}

export interface Approval {
  kind: 'invoke' | 'in_run' | 'input_needed';
  action: string | null;
  target: string | null;
  screenshot: string | null;
  id: string;
  run_id: string;
  connector_id: string;
  reason: string;
  payload: Record<string, unknown>;
  status: 'pending' | 'approved' | 'denied';
  created_at: string;
}

export interface Benchmark {
  connector_id: string;
  explore_ms: number;
  explore_usd: number;
  replay_ms: number;
  replay_usd: number;
  measured_at: string;
  speedup: number;
}

export interface DiffResult {
  connector_id: string;
  base: string;
  head: string;
  healed_from: string | null;
  heal_reason: string | null;
  changes: { step: number; field: string; before: string | null; after: string | null }[];
}

export type ServerEvent =
  | { kind: 'explore.started'; connector_id: string; goal: string }
  | { kind: 'explore.step'; connector_id: string; run_id: string; step: Step }
  | { kind: 'explore.failed'; connector_id: string; reason: string }
  | { kind: 'explore.compiled'; connector_id: string; version: string; run: Run }
  | { kind: 'canary.started'; connector_id: string }
  | { kind: 'canary.passed'; connector_id: string; run: Run }
  | { kind: 'canary.failed'; connector_id: string; failed_step: number | null; reason: string }
  | { kind: 'heal.published'; connector_id: string; version: string }
  | { kind: 'heal.failed'; connector_id: string; reason: string }
  | { kind: 'run.completed'; run: Run }
  | { kind: 'approval.requested'; approval: Approval }
  | { kind: 'approval.decided'; approval: Approval };
