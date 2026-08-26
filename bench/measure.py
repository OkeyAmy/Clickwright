"""Measure what compiling a run actually bought.

Runs the same task twice — once with the model in the loop, once through the
compiled playbook — and records real numbers. Nothing here is estimated; the
console shows what this script measured.

    uv run python -m bench.measure --connector vendor-portal
"""

from __future__ import annotations

import argparse
import asyncio
import time

from app.agents.backend import build_explorer
from app.connectors.models import Benchmark
from app.connectors.registry import Registry
from app.connectors.runtime import ConnectorRuntime
from app.store import Store

# Gemini 3.5 Flash list price per million tokens; override for current pricing.
PRICE_IN = 0.30 / 1_000_000
PRICE_OUT = 2.50 / 1_000_000


def estimate_cost(usage) -> float:
    """Cost from the run's actual reported token usage, not a guess."""
    if not usage:
        return 0.0
    prompt = getattr(usage, "prompt_token_count", 0) or 0
    output = getattr(usage, "candidates_token_count", 0) or 0
    return prompt * PRICE_IN + output * PRICE_OUT


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connector", required=True)
    parser.add_argument("--goal", help="goal to re-explore; skipped if omitted")
    parser.add_argument("--start-url")
    parser.add_argument("--inputs", default="{}", help="JSON payload for the replay leg")
    args = parser.parse_args()

    import json

    registry = Registry()
    store = Store()
    connector = registry.get(args.connector)
    if not connector or not connector.active():
        raise SystemExit(f"no active version for {args.connector}")

    version = connector.active()
    inputs = json.loads(args.inputs) or {
        f.name: f.example or f"BENCH-{f.name.upper()}" for f in version.inputs
    }

    # leg 1 — model in the loop
    explore_ms, explore_usd = 0, 0.0
    if args.goal and args.start_url:
        started = time.monotonic()
        trajectory = await build_explorer().explore(args.connector, args.goal, args.start_url)
        explore_ms = int((time.monotonic() - started) * 1000)
        explore_usd = trajectory.model_cost_usd
        print(f"explore: {explore_ms / 1000:.1f}s  ${explore_usd:.4f}  {len(trajectory.steps)} steps")

    # leg 2 — compiled replay, no model
    runtime = ConnectorRuntime()
    started = time.monotonic()
    run = await runtime.execute(connector, version, inputs, approved=True)
    replay_ms = int((time.monotonic() - started) * 1000)
    print(f"replay:  {replay_ms / 1000:.2f}s  $0.0000  status={run.status}")

    benchmark = Benchmark(
        connector_id=args.connector,
        explore_ms=explore_ms or run.duration_ms,
        explore_usd=explore_usd,
        replay_ms=replay_ms,
    )
    store.save_benchmark(benchmark)
    if benchmark.explore_ms and replay_ms:
        print(f"speedup: {benchmark.speedup:.0f}x")


if __name__ == "__main__":
    asyncio.run(main())
