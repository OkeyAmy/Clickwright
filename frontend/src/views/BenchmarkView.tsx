import { useEffect, useState } from 'react';
import { api } from '../api';
import type { Benchmark, Connector } from '../types';

/** Counts up to a measured value. Seen once per session, so the motion earns its place. */
function useCountUp(target: number, decimals: number) {
  const [value, setValue] = useState(0);

  useEffect(() => {
    if (matchMedia('(prefers-reduced-motion: reduce)').matches) {
      setValue(target);
      return;
    }
    const start = performance.now();
    let frame = 0;
    const tick = (now: number) => {
      const p = Math.min((now - start) / 700, 1);
      setValue(target * (1 - Math.pow(1 - p, 3))); // ease-out
      if (p < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target]);

  return value.toFixed(decimals);
}

function Cards({ benchmark }: { benchmark: Benchmark }) {
  const explore = useCountUp(benchmark.explore_ms / 1000, 1);
  const replay = useCountUp(benchmark.replay_ms / 1000, 2);

  return (
    <div className="bench">
      <article className="card card-warm">
        <span className="card-kicker">Exploration — model in the loop</span>
        <div className="stat"><span className="stat-num">{explore}</span><span className="stat-unit">s</span></div>
        <div className="dim mono">${benchmark.explore_usd.toFixed(4)} per run</div>
        <p className="card-note">
          Gemini reads a screenshot, decides, acts, checks the result. Once — this is the run
          that gets compiled.
        </p>
      </article>

      <div className="arrow" aria-hidden><span /></div>

      <article className="card card-cool">
        <span className="card-kicker">Compiled replay — no model</span>
        <div className="stat"><span className="stat-num">{replay}</span><span className="stat-unit">s</span></div>
        <div className="dim mono">$0 model cost — Cloud Run compute only</div>
        <p className="card-note">
          A deterministic Playwright playbook against the same system. The model only runs
          again if the page changes.
        </p>
      </article>

      <div className="bench-foot">
        <span className="dim">
          <b>{benchmark.speedup.toFixed(0)}× faster</b>, measured on{' '}
          {benchmark.measured_at.replace('T', ' ').replace('Z', '')}.
        </span>
      </div>
    </div>
  );
}

export function BenchmarkView({ connectors }: { connectors: Connector[] | null }) {
  const [benchmark, setBenchmark] = useState<Benchmark | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [measured, setMeasured] = useState<Connector | null>(null);
  const first = measured ?? connectors?.[0];

  // Not every connector has been measured; show one that has rather than
  // reporting "no measurement" because the first one alphabetically lacks it.
  useEffect(() => {
    if (!connectors?.length) return;
    let cancelled = false;
    (async () => {
      for (const connector of connectors) {
        try {
          const found = await api.benchmark(connector.id);
          if (cancelled) return;
          setBenchmark(found);
          setMeasured(connector);
          return;
        } catch (exc) {
          setError((exc as Error).message);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [connectors]);

  if (!first) {
    return (
      <div className="panel">
        <div className="empty"><p>No connectors yet.</p></div>
      </div>
    );
  }

  if (!benchmark) {
    return (
      <div className="panel">
        <div className="empty">
          <p>No measurement recorded for {first.id}.</p>
          <p className="dim">
            Run <code className="mono">python -m bench.measure --connector {first.id}</code> — the
            numbers shown here are always measured, never estimated.
          </p>
          {error && <p className="faint mono">{error}</p>}
        </div>
      </div>
    );
  }

  if (!benchmark.replay_ms) {
    return (
      <div className="panel">
        <div className="empty">
          <p>Exploration measured: {(benchmark.explore_ms / 1000).toFixed(1)}s with the model in the loop.</p>
          <p className="dim">
            The other half arrives the first time something calls{' '}
            <code className="mono">POST {first.path}</code>. Nothing here is estimated.
          </p>
        </div>
      </div>
    );
  }

  return <Cards benchmark={benchmark} />;
}
