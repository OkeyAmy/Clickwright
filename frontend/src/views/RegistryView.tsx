import { useState } from 'react';
import { api } from '../api';
import type { Connector } from '../types';

export function RegistryView({
  connectors,
  error,
  onChange,
}: {
  connectors: Connector[] | null;
  error: string | null;
  onChange: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [spec, setSpec] = useState<{ id: string; body: string } | null>(null);
  const [skill, setSkill] = useState<{ id: string; body: string } | null>(null);

  if (error) return <div className="panel"><div className="empty"><p>{error}</p></div></div>;
  if (!connectors) return <div className="panel"><div className="empty"><p className="dim">Loading…</p></div></div>;

  if (connectors.length === 0) {
    return (
      <div className="panel">
        <div className="empty">
          <p>The registry is empty.</p>
          <p className="dim">Compile a connector from Live exploration and it appears here,
            discoverable by any agent that can read an OpenAPI document.</p>
        </div>
      </div>
    );
  }

  async function runCanary(id: string) {
    setBusy(id);
    try {
      await api.heal(id);
    } catch {
      // 409: a canary is already running for this connector — the live pill
      // and the events stream are already showing it. Nothing to add here.
    } finally {
      setBusy(null);
      onChange();
    }
  }

  async function showSpec(id: string) {
    const body = await api.openapi(id);
    setSpec({ id, body: JSON.stringify(body, null, 2) });
  }

  async function showSkill(id: string) {
    const body = await api.skill(id);
    setSkill({ id, body: body.skill_md });
  }

  return (
    <div style={{ display: 'grid', gap: 16 }}>
      <div className="panel">
        <div className="panel-head">
          <span className="panel-label">Connector registry</span>
          <span className="mono dim">
            {connectors.length} connectors ·{' '}
            {connectors.reduce((n, c) => n + c.versions.length, 0)} versions
          </span>
        </div>
        <div className="tbl-wrap">
          <table className="tbl">
            <thead>
              <tr>
                <th>Connector</th><th>Operation</th><th>Versions</th>
                <th>Inputs</th><th>Identity</th><th />
              </tr>
            </thead>
            <tbody>
              {connectors.map((c) => {
                const active = c.versions.find((v) => v.status === 'active');
                return (
                  <tr key={c.id}>
                    <td>
                      <b>{c.id}</b>
                      <div className="dim" style={{ fontSize: '.76rem' }}>{c.portal}</div>
                    </td>
                    <td className="mono">{c.method} {c.path}</td>
                    <td>
                      {c.versions.map((v) => (
                        <span
                          key={v.version}
                          className={`tag ${v.status === 'active' ? 'tag-active' : ''} ${v.healed_from ? 'tag-healed' : ''}`}
                          title={v.heal_reason ?? v.status}
                        >
                          {v.version}{v.healed_from ? ` ← ${v.healed_from}` : ''}
                        </span>
                      ))}
                    </td>
                    <td className="mono dim">{active?.inputs.map((i) => i.name).join(', ') || '—'}</td>
                    <td className="mono dim">{c.owner}</td>
                    <td>
                      <div className="row">
                        <button className="btn btn-sm" onClick={() => showSpec(c.id)}>Spec</button>
                        <button className="btn btn-sm" onClick={() => showSkill(c.id)}>Skill</button>
                        <button
                          className="btn btn-sm"
                          disabled={busy === c.id}
                          onClick={() => runCanary(c.id)}
                        >
                          {busy === c.id ? 'Running…' : 'Canary'}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      {spec && (
        <div className="panel">
          <div className="panel-head">
            <span className="panel-label">OpenAPI — {spec.id}</span>
            <button className="btn btn-sm" onClick={() => setSpec(null)}>Close</button>
          </div>
          <div className="panel-body">
            <pre className="payload">{spec.body}</pre>
            <p className="dim" style={{ fontSize: '.8rem', marginBottom: 0 }}>
              This is what a consuming agent loads through ADK's <code>OpenAPIToolset</code>.
              The <code>servers</code> block is the only place the base URL can come from.
            </p>
          </div>
        </div>
      )}

      {skill && (
        <div className="panel">
          <div className="panel-head">
            <span className="panel-label">Skill (SKILL.md) — {skill.id}</span>
            <button className="btn btn-sm" onClick={() => setSkill(null)}>Close</button>
          </div>
          <div className="panel-body">
            <pre className="payload">{skill.body}</pre>
            <p className="dim" style={{ fontSize: '.8rem', marginBottom: 0 }}>
              The connector as an agent skill — loadable into any agent that reads
              <code> SKILL.md</code>, so a fleet agent can discover and call it by name.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
