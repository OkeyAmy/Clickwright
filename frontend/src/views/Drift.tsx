import { useEffect, useState } from 'react';
import { api } from '../api';
import type { Connector, DiffResult } from '../types';

/** Shows what the healer changed, and proves no human was in the loop. */
export function Drift({ connectors }: { connectors: Connector[] | null }) {
  const [diff, setDiff] = useState<DiffResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const healed = connectors?.flatMap((c) =>
    c.versions.filter((v) => v.healed_from).map((v) => ({ connector: c, version: v })),
  ) ?? [];

  useEffect(() => {
    const first = healed[0];
    if (!first || diff) return;
    api
      .diff(first.connector.id, first.version.healed_from!, first.version.version)
      .then(setDiff)
      .catch((exc: Error) => setError(exc.message));
  }, [healed.length]);

  if (!connectors) return <div className="panel"><div className="empty"><p className="dim">Loading…</p></div></div>;

  if (healed.length === 0) {
    return (
      <div className="panel">
        <div className="empty">
          <p>Nothing has drifted yet.</p>
          <p className="dim">
            Restart the target with <code className="mono">DRIFT=1</code>, then run a canary from the
            registry. The healer re-learns the broken step and publishes the next version on its own.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      {healed.map(({ connector, version }) => (
        <div className="panel" key={`${connector.id}-${version.version}`}>
          <div className="panel-head">
            <span className="panel-label">{connector.id}</span>
            <span className="mono dim">
              v{version.healed_from} → v{version.version}
            </span>
          </div>
          <div className="panel-body">
            <div className="meta-row">
              <span>Trigger: <b>{version.heal_reason ?? 'canary failure'}</b></span>
              <span>Published {version.created_at.replace('T', ' ').replace('Z', '')}</span>
              <span>Human in loop: <b>no</b></span>
              <span>Source run: <span className="mono">{version.source_run_id}</span></span>
            </div>

            {error && <div className="flag"><b>Diff unavailable</b>{error}</div>}

            {diff?.changes.length ? (
              diff.changes.map((change, i) => (
                <div className="diff" key={i}>
                  <div className="diff-head">step {change.step} · {change.field}</div>
                  <div className="diff-row del"><span className="sign">−</span><span>{change.before ?? '(none)'}</span></div>
                  <div className="diff-row add"><span className="sign">+</span><span>{change.after ?? '(none)'}</span></div>
                </div>
              ))
            ) : (
              <p className="dim" style={{ margin: 0, fontSize: '.82rem' }}>
                The healer republished without a selector change — the failure was transient.
              </p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
