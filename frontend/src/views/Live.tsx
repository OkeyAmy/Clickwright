import { useEffect, useRef, useState } from 'react';
import { api, artifactUrl } from '../api';
import { StepItem } from '../components/StepItem';
import type { Step } from '../types';
import type { Status } from '../App';

/** The bundled target, so a first run works with nothing else set up. */
const EXAMPLE = {
  start_url: 'http://localhost:8081/vendor/login',
  goal:
    'Sign in, then file a Travel expense claim for invoice INV-2026-Q3-4471, ' +
    'amount 284.50, cost centre CC-4410.',
};

export function Live({
  steps,
  runId,
  status,
  setStatus,
}: {
  steps: Step[];
  runId: string | null;
  status: Status;
  setStatus: (s: Status) => void;
}) {
  const [startUrl, setStartUrl] = useState(EXAMPLE.start_url);
  const [goal, setGoal] = useState(EXAMPLE.goal);
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [extraHosts, setExtraHosts] = useState('');
  const [showAuth, setShowAuth] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accepted, setAccepted] = useState<{ connector_id: string; allowed_hosts: string[] } | null>(null);
  const streamRef = useRef<HTMLOListElement>(null);

  // follow the run as it arrives, without stealing scroll from a reader
  useEffect(() => {
    const list = streamRef.current;
    if (!list) return;
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    if (atBottom) list.scrollTo({ top: list.scrollHeight, behavior: 'smooth' });
  }, [steps.length]);

  const latest = [...steps].reverse().find((s) => s.screenshot);
  const running = status.state === 'running';
  const host = safeHost(startUrl);

  async function launch() {
    setError(null);
    try {
      const result = await api.explore({
        start_url: startUrl,
        goal,
        allowed_hosts: extraHosts
          ? extraHosts.split(',').map((h) => h.trim()).filter(Boolean)
          : undefined,
        username: username || undefined,
        password: password || undefined,
      });
      setAccepted(result);
      setPassword(''); // it lives in Secret Manager now, not in this form
      setStatus({ state: 'running', text: 'starting…' });
    } catch (exc) {
      setError((exc as Error).message);
    }
  }

  return (
    <div className="split">
      <div className="panel">
        <div className="panel-head">
          <span className="panel-label">Browser</span>
          <span className="mono dim">{latest?.url ?? startUrl}</span>
        </div>
        <div className="viewport">
          {latest && runId ? (
            <img src={artifactUrl(runId, latest.screenshot!)} alt={`Step ${latest.index}`} />
          ) : latest ? (
            <p className="dim">Waiting for the first frame of this run.</p>
          ) : (
            <div className="empty">
              <p>Nothing is being driven yet.</p>
              <p className="dim">
                Give it the address of a site that has no API, and say what you want done.
                It reads the page and decides — no scraper, no recorded macro, no integration.
              </p>
            </div>
          )}
        </div>
      </div>

      <div style={{ display: 'grid', gap: 16 }}>
        <div className="panel">
          <div className="panel-head">
            <span className="panel-label">Point it at a site</span>
            {host && <span className="mono dim">{host}</span>}
          </div>
          <div className="panel-body form-grid">
            <div className="field">
              <label htmlFor="url">Address</label>
              <input
                id="url"
                placeholder="https://portal.example.com/login"
                value={startUrl}
                onChange={(e) => setStartUrl(e.target.value)}
              />
            </div>

            <div className="field">
              <label htmlFor="goal">What should it do?</label>
              <textarea id="goal" rows={4} value={goal} onChange={(e) => setGoal(e.target.value)} />
            </div>

            <button
              className="btn btn-sm"
              onClick={() => setShowAuth((v) => !v)}
              aria-expanded={showAuth}
            >
              {showAuth ? 'Hide sign-in details' : 'Add sign-in details'}
            </button>

            {showAuth && (
              <>
                <div className="field">
                  <label htmlFor="user">Username</label>
                  <input id="user" autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} />
                </div>
                <div className="field">
                  <label htmlFor="pass">Password</label>
                  <input id="pass" type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} />
                </div>
                <div className="field">
                  <label htmlFor="scope">Also allow these hosts (optional)</label>
                  <input
                    id="scope"
                    placeholder=".example.com, sso.example.com"
                    value={extraHosts}
                    onChange={(e) => setExtraHosts(e.target.value)}
                  />
                </div>
                <p className="dim" style={{ margin: 0, fontSize: '.78rem' }}>
                  Credentials go to Secret Manager and are typed into the browser directly. The
                  model is told to type <code className="mono">{'{{password}}'}</code> and never
                  sees the value. Navigation is refused outside{' '}
                  <b>{host || 'the target host'}</b>.
                </p>
              </>
            )}

            <div className="row">
              <button className="btn btn-primary" onClick={launch} disabled={running || !startUrl || !goal}>
                {running ? 'Working…' : 'Start'}
              </button>
              <span className="dim">{steps.length} steps</span>
            </div>

            {accepted && (
              <p className="dim" style={{ margin: 0, fontSize: '.78rem' }}>
                Compiling as <b>{accepted.connector_id}</b>, scoped to{' '}
                <span className="mono">{accepted.allowed_hosts.join(', ')}</span>.
              </p>
            )}
            {error && <div className="flag"><b>Failed</b>{error}</div>}
            {!error && status.state === 'failed' && (
              <div className="flag"><b>Stopped</b>{status.text}</div>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <span className="panel-label">Action stream</span>
            <span className="mono dim">{runId ?? 'live'}</span>
          </div>
          {steps.length === 0 ? (
            <div className="empty">
              <p>No actions yet.</p>
              <p className="dim">Each step shows what the model did and why.</p>
            </div>
          ) : (
            <ol className="stream" ref={streamRef}>
              {steps.map((step) => (
                <StepItem key={step.index} step={step} mode="explore" />
              ))}
            </ol>
          )}
        </div>
      </div>
    </div>
  );
}

function safeHost(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}
