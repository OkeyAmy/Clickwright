import { useState } from 'react';
import { api } from '../api';
import { useResource } from '../useEvents';
import { StepItem } from '../components/StepItem';
import type { Run } from '../types';

export function Runs() {
  const { data: runs, error } = useResource(() => api.runs(), []);
  const [selected, setSelected] = useState<Run | null>(null);

  if (error) return <div className="panel"><div className="empty"><p>{error}</p></div></div>;
  if (!runs) return <div className="panel"><div className="empty"><p className="dim">Loading…</p></div></div>;

  if (runs.length === 0) {
    return (
      <div className="panel">
        <div className="empty">
          <p>No runs recorded.</p>
          <p className="dim">Explorations, replays and canaries all land here with their full step list.</p>
        </div>
      </div>
    );
  }

  const run = selected ?? runs[0];

  return (
    <div className="split">
      <div className="panel">
        <div className="panel-head">
          <span className="panel-label">Run — {run.id}</span>
          <span className="mono dim">
            {run.mode} · {(run.duration_ms / 1000).toFixed(2)}s ·{' '}
            {run.model_cost_usd > 0 ? `$${run.model_cost_usd.toFixed(4)}` : '$0 model cost'}
          </span>
        </div>
        {run.error && (
          <div className="panel-body">
            <div className="flag">
              <b>Failed at step {run.failed_step ?? '?'}</b>
              {run.error}
            </div>
          </div>
        )}
        <ol className="stream" style={{ maxHeight: 'none' }}>
          {run.steps.map((step) => (
            <StepItem key={step.index} step={step} mode={run.mode} flags={run.policy_events} />
          ))}
        </ol>
      </div>

      <div className="panel">
        <div className="panel-head">
          <span className="panel-label">History</span>
          <span className="mono dim">{runs.length}</span>
        </div>
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr><th>Run</th><th>Mode</th><th>Status</th><th>Time</th></tr>
            </thead>
            <tbody>
              {runs.map((r) => (
                <tr
                  key={r.id}
                  onClick={() => setSelected(r)}
                  style={{ cursor: 'pointer' }}
                >
                  <td className="mono">{r.id}<div className="faint">{r.connector_id}</div></td>
                  <td>{r.mode}</td>
                  <td>
                    <span className={`tag ${r.status === 'ok' ? 'tag-active' : 'tag-danger'}`}>
                      {r.status}
                    </span>
                  </td>
                  <td className="mono dim">{(r.duration_ms / 1000).toFixed(2)}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
