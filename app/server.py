"""Clickwright API and connector gateway.

Two surfaces in one service:
  * /api/*                      — the console reads this
  * /connectors/{id}/{op}       — what other agents call, described by the
                                  OpenAPI documents the registry publishes

Every connector call goes through the same policy gateway, so there is exactly
one place where an action can be held, blocked or audited.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.computer import hosts
from app.agents.distiller import Distiller
from app.connectors.models import ApprovalRequest, Benchmark, Connector, RunRecord
from app.connectors.registry import Registry, diff
from app.connectors.runtime import ConnectorRuntime
from app.governance.gate import ApprovalGate
from app.governance.secrets import store_credentials
from app.store import Store
from app.telemetry import span, setup_telemetry

ARTIFACTS = Path(os.getenv("CLICKWRIGHT_HOME", "var")) / "artifacts"
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
RUNTIME_URL = os.getenv("CLICKWRIGHT_RUNTIME_URL", "http://localhost:8080")

registry = Registry()
store = Store()
runtime = ConnectorRuntime(headless=os.getenv("HEADFUL", "0") != "1")
events: "EventBus"
gate: "ApprovalGate"


class EventBus:
    """Fan-out for live run events. The console subscribes over SSE."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        message = {"kind": kind, **payload}
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                self.unsubscribe(queue)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global events, gate
    events = EventBus()
    gate = ApprovalGate(
        publish=lambda kind, payload: events.publish(kind, payload),
        record=store.save_approval,
    )
    setup_telemetry()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="Clickwright", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── console API ──────────────────────────────────────────────────────────


@app.get("/api/connectors")
def list_connectors() -> list[dict]:
    return [
        {
            **connector.model_dump(),
            "path": connector.path,
            "active_version": connector.active().version if connector.active() else None,
        }
        for connector in registry.list()
    ]


@app.get("/api/connectors/{connector_id}")
def get_connector(connector_id: str) -> dict:
    connector = _require(connector_id)
    return {**connector.model_dump(), "path": connector.path}


@app.get("/api/connectors/{connector_id}/openapi")
def connector_openapi(connector_id: str) -> dict:
    return registry.openapi(_require(connector_id), RUNTIME_URL)


@app.get("/api/connectors/{connector_id}/skill")
def connector_skill(connector_id: str) -> dict:
    return {"skill_md": registry.skill_md(_require(connector_id))}


@app.get("/api/connectors/{connector_id}/diff")
def connector_diff(connector_id: str, base: str, head: str) -> dict:
    connector = _require(connector_id)
    a, b = connector.get(base), connector.get(head)
    if not a or not b:
        raise HTTPException(404, "unknown version")
    return {
        "connector_id": connector_id,
        "base": base,
        "head": head,
        "healed_from": b.healed_from,
        "heal_reason": b.heal_reason,
        "changes": diff(a, b),
    }


@app.get("/api/runs")
def list_runs(connector_id: Optional[str] = None, limit: int = 50) -> list[dict]:
    runs = store.runs_for(connector_id, limit) if connector_id else store.recent_runs(limit)
    return [r.model_dump() for r in runs]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str) -> dict:
    run = store.runs.get(run_id)
    if not run:
        raise HTTPException(404, "unknown run")
    return run.model_dump()


@app.get("/api/benchmarks/{connector_id}")
def get_benchmark(connector_id: str) -> dict:
    benchmark = store.benchmark(connector_id)
    if not benchmark:
        raise HTTPException(404, "no benchmark recorded yet")
    return {**benchmark.model_dump(), "speedup": round(benchmark.speedup, 1)}


@app.get("/api/approvals")
def list_approvals() -> list[dict]:
    return [a.model_dump() for a in store.pending_approvals()]


class AnswerRequest(BaseModel):
    """What the operator typed in answer to the agent's question."""

    value: str


@app.post("/api/approvals/{approval_id}/answer")
def answer_approval(approval_id: str, body: AnswerRequest) -> dict:
    """Hand a paused run the value it asked for — a code, a choice, a detail.

    The value goes straight to the waiting run and is never stored: the audit
    trail keeps the question and that it was answered, not the answer.
    """
    request = store.approvals.get(approval_id)
    if not request or request.status != "pending":
        raise HTTPException(404, "no pending request with that id")
    if not gate.provide(approval_id, body.value):
        raise HTTPException(409, "nothing is waiting on that request any more")
    return {"id": approval_id, "answered": True}


@app.post("/api/approvals/{approval_id}/{decision}")
async def decide_approval(approval_id: str, decision: str) -> dict:
    if decision not in {"approve", "deny"}:
        raise HTTPException(400, "decision must be approve or deny")
    request = store.approvals.get(approval_id)
    if not request or request.status != "pending":
        raise HTTPException(404, "no pending approval with that id")

    # An agent paused mid-run is waiting on this answer; the gate resumes it and
    # records the outcome itself. Only a pre-flight hold has a replay to start.
    if gate.decide(approval_id, decision == "approve"):
        return {**request.model_dump(), "status": "approved" if decision == "approve" else "denied"}

    request.status = "approved" if decision == "approve" else "denied"
    store.save_approval(request)

    if request.status == "denied":
        return request.model_dump()

    connector = _require(request.connector_id)
    run = await runtime.execute(
        connector, connector.active(), request.payload, approved=True
    )
    store.save_run(run)
    events.publish("run.completed", {"run": run.model_dump()})
    return {**request.model_dump(), "run_id": run.id}


@app.get("/api/artifacts/{run_id}/{name}")
def artifact(run_id: str, name: str) -> FileResponse:
    path = ARTIFACTS / run_id / name
    if not path.is_file():
        raise HTTPException(404, "no such artifact")
    return FileResponse(path)


@app.get("/api/events")
async def stream_events(request: Request) -> StreamingResponse:
    queue = events.subscribe()

    async def generator() -> AsyncIterator[str]:
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(message)}\n\n"
        finally:
            events.unsubscribe(queue)

    return StreamingResponse(generator(), media_type="text/event-stream")


# ── exploration & healing (long-running, kicked off in the background) ───


class ExploreRequest(BaseModel):
    """Point it at a site. Everything except the URL and the goal is optional."""

    start_url: str
    goal: str
    connector_id: Optional[str] = None
    portal: Optional[str] = None
    operation: Optional[str] = None
    # Defaults to the target's own host. Use ".example.com" to include subdomains.
    allowed_hosts: Optional[list[str]] = None
    # Stored in Secret Manager (or an owner-only local file) and injected into
    # the browser. Never echoed back, never placed in model context.
    username: Optional[str] = None
    password: Optional[str] = None


def _derive(body: ExploreRequest) -> ExploreRequest:
    """Fill in the identity of a target from its URL, so the operator only has
    to supply the two things they actually know."""
    host = hosts.host_of(body.start_url)
    if not host:
        raise HTTPException(400, f"could not read a host from {body.start_url!r}")

    label = host.removeprefix("www.")
    connector_id = body.connector_id or re.sub(r"[^a-z0-9]+", "-", label).strip("-")
    return body.model_copy(
        update={
            "connector_id": connector_id,
            "portal": body.portal or label,
            "operation": body.operation or _operation_from(body.goal),
            "allowed_hosts": hosts.normalise(body.allowed_hosts, body.start_url),
        }
    )


def _operation_from(goal: str) -> str:
    """A short, stable operation name taken from the goal's opening words."""
    words = re.findall(r"[a-z0-9]+", goal.lower())[:3]
    return "-".join(words) or "task"


@app.post("/api/explore")
async def explore(body: ExploreRequest, background: BackgroundTasks) -> dict:
    body = _derive(body)
    try:
        hosts.check(body.start_url, body.allowed_hosts or [])
    except hosts.HostRefused as exc:
        raise HTTPException(400, str(exc)) from exc

    stored = store_credentials(body.connector_id, body.username, body.password)
    # drop them from the object that crosses into the background task
    body = body.model_copy(update={"username": None, "password": None})

    background.add_task(_explore_and_compile, body)
    return {
        "accepted": True,
        "connector_id": body.connector_id,
        "operation": body.operation,
        "allowed_hosts": body.allowed_hosts,
        "credentials_stored": stored,
    }


def _explain(exc: Exception) -> str:
    """One line a person can act on, out of an SDK exception with a page of JSON."""
    text = str(exc)
    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        retry = re.search(r"retry in ([\d.]+)s", text, re.IGNORECASE)
        model = re.search(r"model: ([\w.-]+)", text)
        wait = f" Retry in about {float(retry.group(1)):.0f}s." if retry else ""
        which = f" ({model.group(1)})" if model else ""
        return f"Gemini API quota exhausted{which}.{wait}"
    first = text.strip().splitlines()[0] if text.strip() else type(exc).__name__
    return f"{type(exc).__name__}: {first}"[:300]


async def _explore_and_compile(body: ExploreRequest) -> None:
    from app.agents.backend import build_explorer
    from app.agents.distiller import Distiller

    events.publish(
        "explore.started",
        {"connector_id": body.connector_id, "goal": body.goal, "start_url": body.start_url},
    )
    def publish_step(run_id: str, step) -> None:
        events.publish(
            "explore.step",
            {"connector_id": body.connector_id, "run_id": run_id, "step": step.model_dump()},
        )

    try:
        with span("explore", connector_id=body.connector_id):
            explorer = build_explorer(artifacts_dir=ARTIFACTS, gate=gate)
            trajectory = await explorer.explore(
                body.connector_id,
                body.goal,
                body.start_url,
                body.allowed_hosts,
                on_step=publish_step,
            )

        origin = urlparse(body.start_url)
        connector = registry.get(body.connector_id) or Connector(
            id=body.connector_id,
            portal=body.portal,
            operation=body.operation,
            base_url=f"{origin.scheme}://{origin.netloc}",
            allowed_hosts=body.allowed_hosts or [],
            owner=f"sa-connector-{body.connector_id}@clickwright.iam",
        )
        # The distiller calls its provider synchronously; off the event loop,
        # or a slow model freezes every endpoint — SSE, healthz, all of it.
        version = await asyncio.to_thread(
            Distiller().compile, trajectory, version=connector.bump()
        )
        registry.publish(connector, version)

        run = RunRecord(
            id=trajectory.run_id,
            connector_id=body.connector_id,
            mode="explore",
            version=version.version,
            duration_ms=trajectory.duration_ms,
            model_cost_usd=trajectory.model_cost_usd,
            steps=trajectory.steps,
        )
        store.save_run(run)
        # Half of the benchmark: what it cost to work the system out. The other
        # half arrives the first time the compiled connector is called.
        await asyncio.to_thread(
            store.save_benchmark,
            Benchmark(
                connector_id=body.connector_id,
                explore_ms=trajectory.duration_ms,
                explore_usd=trajectory.model_cost_usd,
                replay_ms=0,
            ),
        )
        events.publish(
            "explore.compiled",
            {"connector_id": body.connector_id, "version": version.version, "run": run.model_dump()},
        )
    except Exception as exc:
        # This runs in a background task: without an event the console sits on
        # "Working…" forever and the reason is only ever in the server log.
        events.publish(
            "explore.failed",
            {"connector_id": body.connector_id, "reason": _explain(exc)},
        )
        raise


# One canary/heal per connector at a time. A second click while one is
# running would spawn a twin exploration and race it to the registry.
_heal_inflight: set[str] = set()


@app.post("/api/heal/{connector_id}")
async def heal(connector_id: str, background: BackgroundTasks) -> dict:
    _require(connector_id)
    if connector_id in _heal_inflight:
        raise HTTPException(409, f"a canary or heal for {connector_id!r} is already running")
    _heal_inflight.add(connector_id)
    background.add_task(_run_heal, connector_id)
    return {"accepted": True, "connector_id": connector_id}


async def _run_heal(connector_id: str) -> None:
    try:
        await _heal(connector_id)
    finally:
        _heal_inflight.discard(connector_id)


async def _heal(connector_id: str) -> None:
    from app.agents.healer import Healer

    healer = Healer(registry=registry, runtime=runtime, artifacts_dir=ARTIFACTS)
    connector = registry.get(connector_id)
    events.publish("canary.started", {"connector_id": connector_id})

    try:
        with span("canary", connector_id=connector_id):
            result = await healer.check(connector)
        store.save_run(result.run)

        if result.healthy:
            events.publish("canary.passed", {"connector_id": connector_id, "run": result.run.model_dump()})
            return

        events.publish(
            "canary.failed",
            {"connector_id": connector_id, "failed_step": result.run.failed_step, "reason": result.reason},
        )

        # An unreachable target is nobody's playbook problem: escalating to
        # computer use against a dead network only burns quota to rediscover it.
        if not result.healable:
            events.publish("heal.failed", {"connector_id": connector_id, "reason": result.reason})
            return

        with span("heal", connector_id=connector_id):
            healed = await healer.heal(connector, result)

        # A heal that changed nothing is a real answer, not a success: without
        # this event the console would sit on the red canary forever.
        if healed.healed_to:
            events.publish("heal.published", {"connector_id": connector_id, "version": healed.healed_to})
        else:
            events.publish(
                "heal.failed",
                {"connector_id": connector_id, "reason": healed.reason or "the heal produced no change"},
            )
    except Exception as exc:
        # This runs in a background task: without an event the console sits on
        # "canary failed…" forever and the reason is only ever in the server log.
        events.publish("heal.failed", {"connector_id": connector_id, "reason": _explain(exc)})
        raise


# ── the connector gateway: what other agents call ───────────────────────


@app.post("/connectors/{connector_id}/{operation}")
async def invoke(connector_id: str, operation: str, payload: dict[str, Any]) -> dict:
    connector = _require(connector_id)
    if connector.operation != operation:
        raise HTTPException(404, f"{connector_id} exposes {connector.operation}, not {operation}")
    version = connector.active()
    if not version:
        raise HTTPException(409, f"{connector_id} has no active version")

    with span("invoke", connector_id=connector_id, version=version.version):
        run = await runtime.execute(connector, version, payload)
    store.save_run(run)
    _record_replay_cost(connector_id, run)

    if run.status == "held_for_approval":
        request = ApprovalRequest(
            id=f"apr_{uuid.uuid4().hex[:6]}",
            run_id=run.id,
            connector_id=connector_id,
            reason=run.result.get("reason", "policy hold"),
            payload=payload,
        )
        store.save_approval(request)
        events.publish("approval.requested", {"approval": request.model_dump()})
        return {
            "status": "held_for_approval",
            "approval_id": request.id,
            "reason": request.reason,
            "run_id": run.id,
        }

    events.publish("run.completed", {"run": run.model_dump()})
    if run.status == "failed":
        raise HTTPException(
            502, {"error": run.error, "failed_step": run.failed_step, "run_id": run.id}
        )
    return {**run.result, "run_id": run.id, "version": version.version}


def _record_replay_cost(connector_id: str, run: RunRecord) -> None:
    """The other half of the benchmark: what the compiled connector costs.

    Only a clean replay counts — a failed run measures how long it took to hit
    the failure, which is not the number anyone is asking about.
    """
    if run.status != "ok" or not run.duration_ms:
        return
    existing = store.benchmark(connector_id)
    if not existing:
        return
    store.save_benchmark(
        existing.model_copy(update={"replay_ms": run.duration_ms, "replay_usd": 0.0})
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "connectors": len(registry.list())}


def _require(connector_id: str) -> Connector:
    connector = registry.get(connector_id)
    if not connector:
        raise HTTPException(404, f"no connector {connector_id!r}")
    return connector


# ── console assets ───────────────────────────────────────────────────────
# Mounted last: a mount at "/" matches everything, so it has to sit behind
# every explicit route or it swallows the API and the health check.

if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="console")
