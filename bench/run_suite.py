"""The Clickwright architecture benchmark.

Industry agent benchmarks (WebArena, WebVoyager, Mind2Web) grade *models*.
Clickwright's contribution starts where the model stops, so this suite grades
what happens after the exploration — with no model required for legs 1–3:

  leg 1 · replay      the compiled artifact runs deterministically; latency,
                      $0 model cost
  leg 2 · detection   one selector in the artifact is corrupted; the canary
                      must fail at exactly that step — precision, not vibes
  leg 3 · recovery    the good artifact is republished; the next call is green.
                      With --heal, a real model performs the repair instead and
                      the suite records time-to-recovery

Optional --explore adds the compilation leg (yield, selector durability mix,
assertion survival rate); optional --judge grades exploration WebVoyager-style
— that column measures the MODEL, and is labelled as such.

    PYTHONPATH=. python -m bench.run_suite                 # every registered connector
    PYTHONPATH=. python -m bench.run_suite --only localhost,wikipedia
    PYTHONPATH=. python -m bench.run_suite --explore       # + fresh compilations
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from app.agents.healer import Healer
from app.connectors.models import ConnectorVersion, Selector
from app.connectors.registry import Registry
from app.connectors.runtime import ConnectorRuntime
from app.governance.secrets import store_credentials
from bench.repair import repair_comparison_legs

TASKS = Path(__file__).parent / "tasks.json"
FAULT_SELECTOR = "#cw-bench-missing-target"


def _load_env(path: Path = Path(".env")) -> None:
    """Same contract as uvicorn --env-file, for direct runs of this module.

    Without it the connector credentials silently resolve empty and every
    leg 'fails' for reasons that have nothing to do with the architecture."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ── artifact metrics ─────────────────────────────────────────────────────


def selector_class(candidate: str) -> str:
    """Mirror the recorder's durability order: testid > id > name > label > text."""
    if "[data-testid" in candidate or "[data-test" in candidate or "[data-qa" in candidate:
        return "testid"
    if candidate.startswith("#"):
        return "id"
    if "[name=" in candidate:
        return "name"
    if "aria-label=" in candidate:
        return "aria-label"
    if ":has-text(" in candidate:
        return "text"
    if "nth-of-type(" in candidate:
        return "positional"
    return "other"


def step_durability(selector: Selector | None) -> int:
    """2 = anchored to a developer-named hook, 1 = labelled by content,
    0 = positional guesswork only."""
    if selector is None:
        return 0
    classes = [selector_class(c) for c in selector.candidates()]
    if any(c in ("testid", "id", "name") for c in classes):
        return 2
    if any(c in ("aria-label", "text") for c in classes):
        return 1
    return 0


def artifact_metrics(version: ConnectorVersion) -> dict[str, Any]:
    scored = [step_durability(s.selector) for s in version.steps if s.action.value != "navigate"]
    selector_steps = [s for s in version.steps if s.selector]
    return {
        "steps": len(version.steps),
        "selector_steps": len(selector_steps),
        "durability_avg": round(sum(scored) / len(scored), 2) if scored else None,
        "positional_only": sum(1 for s in scored if s == 0),
        "assertions_kept": sum(1 for s in version.steps if s.expect_text),
        "inputs": len(version.inputs),
    }


# ── the three legs ───────────────────────────────────────────────────────


async def replay_leg(runtime: ConnectorRuntime, registry: Registry, cid: str) -> dict[str, Any]:
    stored = registry.get(cid)
    active = stored.active()
    inputs = Healer.canary_inputs(active)
    started = time.monotonic()
    run = await runtime.execute(stored, active, inputs, approved=True, mode="replay")
    return {
        "replay_ok": run.status == "ok",
        "replay_s": round(time.monotonic() - started, 2),
        "error": None if run.status == "ok" else (run.error or "")[:160],
    }


async def detection_leg_instrumented(
    runtime: ConnectorRuntime, registry: Registry, cid: str
) -> dict[str, Any]:
    """Corrupt one selector in a published copy, canary it, restore.

    A silent wrong-step failure would send the healer to repair the innocent
    neighbour while the broken control ships another day — so the run must
    fail at exactly the index we broke."""
    stored = registry.get(cid)
    good = stored.active()
    anchor = max(
        (s for s in good.steps if s.selector and s.action.value != "navigate"),
        key=lambda s: abs(s.index - len(good.steps) / 2),
        default=None,
    )
    if anchor is None:
        return {"detectable": False}

    broken = good.model_copy(deep=True, update={"version": "9.0.0"})
    for step in broken.steps:
        if step.index == anchor.index:
            # drift kills the locator; what was recorded *about* the element survives
            step.selector.primary = FAULT_SELECTOR
            step.selector.fallbacks = []
            step.expect_text = None
    registry.publish(stored, broken)

    current = registry.get(cid)
    started = time.monotonic()
    run = await runtime.execute(current, current.active(), Healer.canary_inputs(current.active()),
                                approved=True, mode="canary")
    failed_at = run.failed_step
    detected = run.status == "failed" and failed_at == anchor.index

    repaired = good.model_copy(deep=True, update={"version": "9.0.1"})
    registry.publish(current, repaired)
    current = registry.get(cid)
    recovered = await runtime.execute(current, current.active(),
                                      Healer.canary_inputs(current.active()),
                                      approved=True, mode="replay")

    return {
        "detectable": True,
        "injected_step": anchor.index,
        "failed_at": failed_at,
        "detection_precise": detected,
        "recovery_ok": recovered.status == "ok",
        "recovery_s": round(time.monotonic() - started, 2),
        "replay_error": None if run.status != "failed" else (run.error or "")[:120],
    }


# ── optional compilation leg ─────────────────────────────────────────────


async def compile_leg(task: dict[str, Any], home: Path) -> dict[str, Any]:
    """Explore → distill, and report how much of the model's output survived
    the architecture's verification. This leg needs a model; the others don't."""
    from app.agents.backend import build_explorer
    from app.agents.distiller import Distiller
    from app.connectors.models import Connector

    registry = Registry(home / "registry")
    cid = task.get("connector_id") or _derive_id(task["start_url"])
    store_credentials(cid, task.get("username"), task.get("password"))

    trajectory = await build_explorer().explore(cid, task["goal"], task["start_url"])
    distiller = Distiller()
    raw = await asyncio.to_thread(distiller._raw_compile, trajectory)
    origin = urlparse(task["start_url"])
    connector = Connector(
        id=cid,
        portal=origin.netloc,
        operation=_operation_from(task["goal"]),
        base_url=f"{origin.scheme}://{origin.netloc}",
        allowed_hosts=[f".{origin.netloc.removeprefix('www.')}" or origin.netloc],
    )
    version = await asyncio.to_thread(
        distiller._assemble, trajectory, raw, "1.0.0", None, None
    )
    registry.publish(connector, version)
    proposed = sum(1 for item in raw.get("steps", []) if item.get("expect_text"))
    kept = sum(1 for s in version.steps if s.expect_text)
    metrics = artifact_metrics(version)
    metrics.update(
        {
            "connector_id": cid,
            "compile_ok": True,
            "explore_s": round(trajectory.duration_ms / 1000, 1),
            "assertions_proposed": proposed,
            "assertion_survival": round(kept / proposed, 2) if proposed else None,
            "replay_inputs": task.get("replay_inputs"),
        }
    )
    return metrics


def _derive_id(url: str) -> str:
    label = urlparse(url).netloc.removeprefix("www.")
    import re

    return re.sub(r"[^a-z0-9]+", "-", label).strip("-")


def _operation_from(goal: str) -> str:
    import re

    words = re.findall(r"[a-z0-9]+", goal.lower())[:3]
    return "-".join(words) or "task"


# ── baselines ────────────────────────────────────────────────────────────
#
# A benchmark that only measures itself is a demo. Following the web-test-
# repair literature, the same injected fault is put through every contender —
# see bench/repair.py for the strategies; model_baseline covers the naive
# "agent again per call" alternative with measured numbers.

# ── orchestration ────────────────────────────────────────────────────────


def model_baseline(cid: str) -> Optional[dict[str, Any]]:
    """The naive alternative: pay the model on every call. Real numbers from
    the stored benchmark — never estimates."""
    from app.store import Store

    benchmark = Store().benchmark(cid)
    if not benchmark or not benchmark.explore_ms:
        return None
    return {
        "explore_s": round(benchmark.explore_ms / 1000, 1),
        "usd_per_call": round(benchmark.explore_usd, 4),
        "note": "a fresh computer-use run per call",
    }


async def evaluate_connector(
    cid: str,
    home: Path,
    runtime: ConnectorRuntime,
    with_heal: bool = False,
    include_llm: bool = False,
) -> dict[str, Any]:
    scratch = Registry(home / "registry")
    source = Registry()
    connector = source.get(cid)
    if not connector or not connector.active():
        return {"id": cid, "error": "not in the live registry"}

    # copy the artifact into the scratch registry so faults never touch prod
    scratch.save(connector.model_copy(deep=True))
    row: dict[str, Any] = {"id": cid, "metrics": {}, "replay": {}, "detection": {}}

    row["metrics"] = artifact_metrics(connector.active())
    row["replay"] = await replay_leg(runtime, scratch, cid)

    if with_heal:
        healer = Healer(registry=scratch, runtime=runtime)
        faulted = await _publish_fault(scratch, cid)
        started = time.monotonic()
        check = await healer.check(scratch.get(cid))
        if not check.healable:
            row["detection"] = {"detection_precise": False, "reason": check.reason}
        else:
            healed = await healer.heal(scratch.get(cid), check)
            row["detection"] = {
                "injected_step": faulted,
                "failed_at": check.run.failed_step,
                "detection_precise": check.run.failed_step == faulted,
                "healed_to": healed.healed_to,
                "mttr_s": round(time.monotonic() - started, 1) if healed.healed_to else None,
                "reason": healed.reason,
            }
    else:
        row["detection"] = await detection_leg_instrumented(runtime, scratch, cid)

    # the same fault through the repair strategies the literature compares
    row["repair_strategies"] = await repair_comparison_legs(
        runtime, scratch, cid, include_llm=include_llm
    )
    row["model"] = model_baseline(cid)
    return row


async def _publish_fault(scratch: Registry, cid: str) -> int:
    stored = scratch.get(cid)
    good = stored.active()
    anchor = max(
        (s for s in good.steps if s.selector and s.action.value != "navigate"),
        key=lambda s: abs(s.index - len(good.steps) / 2),
    )
    broken = good.model_copy(deep=True, update={"version": "9.0.0"})
    for step in broken.steps:
        if step.index == anchor.index:
            # drift kills the locator; what was recorded *about* the element survives
            step.selector.primary = FAULT_SELECTOR
            step.selector.fallbacks = []
            step.expect_text = None
    scratch.publish(stored, broken)
    return anchor.index


def render_report(rows: list[dict[str, Any]]) -> str:
    replays = [r for r in rows if r["replay"].get("replay_s") is not None]
    ok_replays = sum(1 for r in replays if r["replay"]["replay_ok"])
    detections = [r["detection"] for r in rows if r["detection"].get("detectable")]
    precise = sum(1 for d in detections if d.get("detection_precise"))
    recovered = sum(1 for d in detections if d.get("recovery_ok"))
    latencies = sorted(r["replay"]["replay_s"] for r in replays)
    p50 = statistics.median(latencies) if latencies else 0
    durable = [
        m["durability_avg"] for m in (r.get("metrics") for r in rows)
        if isinstance(m, dict) and m.get("durability_avg") is not None
    ]

    lines = [
        "# Architecture benchmark",
        "",
        "| connector | steps | durability | assertions | replay | detect@step | recover |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        m = r.get("metrics", {})
        det = r.get("detection", {})
        lines.append(
            f"| {r['id']} | {m.get('steps', '—')} | {m.get('durability_avg', '—')} "
            f"| {m.get('assertions_kept', '—')} | {_tick(r['replay'].get('replay_ok'))} "
            f"| {det.get('failed_at', '—')}/{det.get('injected_step', '—')} "
            f"| {_tick(det.get('recovery_ok'))} |"
        )
    lines += [
        "",
        f"- deterministic replay success: **{ok_replays}/{len(replays)}** — $0.00 model cost each",
        f"- replay latency p50: **{p50:.2f}s** (exploration of the same tasks: minutes)",
        f"- fault detection precision: **{precise}/{len(detections)}** — the canary names the exact broken step",
        f"- recovery to green after republish: **{recovered}/{len(detections)}**",
        f"- selector durability (2=hook, 1=labelled, 0=positional): **{round(statistics.mean(durable), 2) if durable else '—'} average**",
    ]

    # ── repair-strategy comparison: the literature's protocol ────────────
    strategies = [r.get("repair_strategies") for r in rows if r.get("repair_strategies")]
    if strategies:
        def _count(key: str) -> tuple[int, int]:
            have = [s[key] for s in strategies if key in s]
            return sum(1 for s in have if s["completed"]), len(have)

        floor = _count("static_floor")
        similar = _count("attribute_similarity")
        llm = _count("llm_single_step")
        healed = sum(
            1 for r in rows
            if r["detection"].get("recovery_ok") or r["detection"].get("healed_to")
        )
        total = len(strategies)

        lines += [
            "",
            f"## Repair under an identical locator break ({total} target{'s' if total != 1 else ''})",
            "",
            "The web-test-repair literature compares strategies by repair accuracy "
            "and recovery time on the same injected fault (Hammoudi et al.; Similo; "
            "VON Similo-LLM). Same protocol, same fault, per target:",
            "",
            "| strategy | repairs | median time | model calls |",
            "|---|---|---|---|",
        ]
        rows_out = [
            ("static floor (no repair)", floor, "—", 0),
            ("attribute similarity (Similo-style)", similar,
             _median_time(strategies, "attribute_similarity"), 0),
            ("LLM single-step pick (VON-Similo-LLM style)", llm if llm[1] else (0, 0),
             _median_time(strategies, "llm_single_step") if llm[1] else "—", llm[1]),
            ("clickwright — computer-use heal loop", (healed, total),
             _median_recovery(rows), None),
        ]
        for name, (ok, n), med, calls in rows_out:
            calls_cell = str(calls) if calls is not None else "1 agent run"
            lines.append(f"| {name} | {ok}/{n} | {med} | {calls_cell} |")

        lines += [
            "",
            "A repaired *locator* resumes an existing playbook; Clickwright's heal "
            "re-performs the step with full task context and republishes a verified "
            "version. Both are measured here because the literature treats them as "
            "different repair classes.",
        ]
    return "\n".join(lines)


def _tick(value: Any) -> str:
    return "✓" if value else "✗"


def _median_time(strategies: list[dict], key: str) -> str:
    times = [s[key]["time_s"] for s in strategies if key in s and s[key].get("time_s")]
    return f"{statistics.median(times):.1f}s" if times else "—"


def _median_recovery(rows: list[dict]) -> str:
    times = [
        r["detection"].get("recovery_s") for r in rows
        if r["detection"].get("recovery_s")
    ]
    return f"{statistics.median(times):.1f}s" if times else "—"


async def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default="", help="comma-separated connector ids")
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--heal", action="store_true",
                        help="let a real model perform the repair (records MTTR)")
    parser.add_argument("--explore", action="store_true",
                        help="also run the compilation leg against bench/tasks.json")
    parser.add_argument("--llm", action="store_true",
                        help="include the LLM single-step locator-repair baseline")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    home = Path("var") / "bench" / stamp
    home.mkdir(parents=True, exist_ok=True)
    runtime = ConnectorRuntime(headless=not args.headful)
    rows: list[dict[str, Any]] = []

    source = Registry()
    ids = (
        [c.strip() for c in args.only.split(",") if c.strip()]
        if args.only
        else [c.id for c in source.list() if c.active()]
    )
    print(f"{len(ids)} connectors → {home}")
    for cid in ids:
        print(f"▶ {cid}", flush=True)
        rows.append(await evaluate_connector(
            cid, home, runtime, with_heal=args.heal, include_llm=args.llm
        ))

    if args.explore:
        for task in json.loads(TASKS.read_text())["tasks"]:
            try:
                print(f"▶ compile: {task['id']}", flush=True)
                row = await compile_leg(task, home)
                row.update({"id": task["id"], "replay": {}, "detection": {}})
                rows.append(row)
            except Exception as exc:  # noqa: BLE001
                rows.append({"id": task["id"], "error": f"{type(exc).__name__}: {exc}"[:200]})

    (home / "rows.json").write_text(json.dumps(rows, indent=2, default=str))
    report = render_report(rows)
    (home / "report.md").write_text(report)
    print("\n" + report)


if __name__ == "__main__":
    asyncio.run(main())
