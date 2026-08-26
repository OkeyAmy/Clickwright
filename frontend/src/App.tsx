import { useCallback, useRef, useState } from 'react';
import { api } from './api';
import { useEvents, useResource } from './useEvents';
import type { ServerEvent, Step } from './types';
import { Live } from './views/Live';
import { RegistryView } from './views/RegistryView';
import { Runs } from './views/Runs';
import { Drift } from './views/Drift';
import { BenchmarkView } from './views/BenchmarkView';
import { Approvals } from './views/Approvals';

type ViewName = 'live' | 'registry' | 'runs' | 'drift' | 'bench' | 'approvals';

const VIEWS: Record<ViewName, { title: string; sub: string }> = {
  live: { title: 'Live exploration', sub: 'A model operating a system that has no API' },
  registry: { title: 'Registry', sub: 'Connectors published for cross-department discovery' },
  runs: { title: 'Runs', sub: 'Every action, its stated reason, and the selector it resolved to' },
  drift: { title: 'Drift', sub: 'The target changed. Nobody was asked to fix it.' },
  bench: { title: 'Benchmark', sub: 'What compiling the run actually bought' },
  approvals: { title: 'Approvals', sub: 'Actions the policy gateway would not take unattended' },
};

export type Status = { state: 'idle' | 'running' | 'ok' | 'failed'; text: string };

/** Event reasons are for the pill, not a log pane: one line that never breaks layout. */
const short = (text: string | null | undefined, limit = 96) => {
  const collapsed = (text ?? '').replace(/\s+/g, ' ').trim();
  return collapsed.length <= limit ? collapsed : collapsed.slice(0, limit - 1) + '…';
};

export default function App() {
  const [view, setView] = useState<ViewName>('live');
  const [status, setStatus] = useState<Status>({ state: 'idle', text: 'idle' });
  const [liveSteps, setLiveSteps] = useState<Step[]>([]);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const runIdRef = useRef<string | null>(null);

  const connectors = useResource(api.connectors, []);
  const approvals = useResource(api.approvals, []);

  const onEvent = useCallback((event: ServerEvent) => {
    switch (event.kind) {
      case 'explore.started':
        setLiveSteps([]);
        setActiveRunId(null);
        runIdRef.current = null;
        setStatus({ state: 'running', text: 'computer use — exploring' });
        break;
      case 'explore.step':
        // the frames are on disk already — the pane can show them mid-run.
        // Two runs at once would otherwise interleave into one nonsense list.
        setLiveSteps((steps) => (event.run_id === runIdRef.current ? [...steps, event.step] : [event.step]));
        runIdRef.current = event.run_id;
        setActiveRunId(event.run_id);
        break;
      case 'explore.failed':
        setStatus({ state: 'failed', text: short(event.reason) });
        break;
      case 'explore.compiled':
        setActiveRunId(event.run.id);
        setStatus({ state: 'ok', text: `compiled → v${event.version}` });
        connectors.refresh();
        break;
      case 'canary.started':
        setStatus({ state: 'running', text: `canary — ${event.connector_id}` });
        break;
      case 'canary.passed':
        setStatus({ state: 'ok', text: 'canary passed' });
        break;
      case 'canary.failed':
        setStatus({
          state: 'failed',
          text:
            event.failed_step != null
              ? `canary failed at step ${event.failed_step}`
              : short(event.reason) || 'canary failed',
        });
        break;
      case 'heal.published':
        setStatus({ state: 'ok', text: `healed → v${event.version}` });
        connectors.refresh();
        break;
      case 'heal.failed':
        setStatus({ state: 'failed', text: short(event.reason) });
        break;
      case 'approval.requested':
        approvals.refresh();
        // a paused run is not a failed run — it is waiting on the person reading this
        setStatus({ state: 'running', text: 'waiting on you — see Approvals' });
        break;
      case 'approval.decided':
        approvals.refresh();
        setStatus({ state: 'running', text: 'resumed' });
        break;
      case 'run.completed':
        setStatus({ state: event.run.status === 'ok' ? 'ok' : 'failed', text: `run ${event.run.status}` });
        approvals.refresh();
        break;
    }
  }, [connectors, approvals]);

  const connected = useEvents(onEvent);
  const meta = VIEWS[view];
  const pending = approvals.data?.length ?? 0;

  return (
    <div className="app">
      <nav className="rail" aria-label="Sections">
        <div className="brand">
          <span className="brand-mark" aria-hidden />
          <span className="brand-name">Clickwright</span>
        </div>

        <ul className="nav">
          {(Object.keys(VIEWS) as ViewName[]).map((name) => (
            <li key={name}>
              <button
                className="nav-item"
                aria-current={view === name ? 'page' : undefined}
                onClick={() => setView(name)}
              >
                {VIEWS[name].title}
                {name === 'live' && status.state === 'running' && <span className="nav-dot" data-on />}
                {name === 'approvals' && pending > 0 && (
                  <span className="nav-count" data-alert>{pending}</span>
                )}
                {name === 'registry' && connectors.data && (
                  <span className="nav-count">{connectors.data.length}</span>
                )}
              </button>
            </li>
          ))}
        </ul>

        <div className="rail-foot">
          <div className="legend">
            <span className="legend-row"><i className="swatch swatch-warm" /> model in the loop</span>
            <span className="legend-row"><i className="swatch swatch-cool" /> compiled replay</span>
          </div>
          <span className="faint mono">{connected ? 'stream live' : 'stream offline'}</span>
        </div>
      </nav>

      <header className="chrome">
        <div>
          <h1>{meta.title}</h1>
          <p className="chrome-sub">{meta.sub}</p>
        </div>
        <div className="chrome-actions">
          {/* title carries the full reason on hover; the pill itself never wraps */}
          <span className="pill" data-state={status.state} title={status.text}>
            <i className="pip" />
            {status.text}
          </span>
        </div>
      </header>

      <main className="stage">
        {view === 'live' && (
          <Live steps={liveSteps} runId={activeRunId} status={status} setStatus={setStatus} />
        )}
        {view === 'registry' && (
          <RegistryView connectors={connectors.data} error={connectors.error} onChange={connectors.refresh} />
        )}
        {view === 'runs' && <Runs />}
        {view === 'drift' && <Drift connectors={connectors.data} />}
        {view === 'bench' && <BenchmarkView connectors={connectors.data} />}
        {view === 'approvals' && (
          <Approvals approvals={approvals.data} error={approvals.error} onChange={approvals.refresh} />
        )}
      </main>
    </div>
  );
}
