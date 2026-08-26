"""Contracts shared by every component.

The dashboard's mock.js mirrors these shapes; keep the two in sync.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class Action(str, Enum):
    """The subset of computer-use actions a playbook can replay deterministically."""

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    WAIT = "wait"
    ASSERT = "assert"


# ── exploration ──────────────────────────────────────────────────────────


class Selector(BaseModel):
    """A resolved element reference, with fallbacks ordered by durability.

    Coordinates alone cannot be replayed — the recorder resolves what was under
    the pointer so the distiller has something to compile.
    """

    primary: str
    fallbacks: list[str] = Field(default_factory=list)
    accessible_name: Optional[str] = None
    text: Optional[str] = None

    def candidates(self) -> list[str]:
        return [self.primary, *self.fallbacks]


class TrajectoryStep(BaseModel):
    index: int
    action: Action
    value: Optional[str] = None
    url: Optional[str] = None
    selector: Optional[Selector] = None
    reason: Optional[str] = None  # the model's stated intent, captured per §4.5b
    submits: bool = False  # the keystroke that sent the form, not just filled it
    ms: int = 0
    screenshot: Optional[str] = None  # gs:// or local path
    status: Literal["ok", "failed"] = "ok"
    # Whitespace-normalised page text as it stood after this step. The distiller
    # checks every proposed assertion against it, so an `expect_text` that only
    # ever existed in the model's imagination never reaches a playbook.
    page_text: Optional[str] = None


class Trajectory(BaseModel):
    run_id: str
    connector_id: str
    goal: str
    model: str
    started_at: str = Field(default_factory=_now)
    duration_ms: int = 0
    model_cost_usd: float = 0.0
    steps: list[TrajectoryStep] = Field(default_factory=list)


# ── the compiled artifact ────────────────────────────────────────────────


class PlaybookStep(BaseModel):
    index: int
    action: Action
    selector: Optional[Selector] = None
    value_from: Optional[str] = None  # name of an input field, e.g. "invoice_ref"
    value: Optional[str] = None  # literal, when not driven by input
    url: Optional[str] = None
    expect_text: Optional[str] = None  # assertion anchoring this step
    submits: bool = False  # replay must press Enter here, or the form never goes
    timeout_ms: int = 8000


class InputField(BaseModel):
    name: str
    type: Literal["string", "number", "boolean"] = "string"
    required: bool = True
    description: str = ""
    example: Optional[str] = None


class ConnectorVersion(BaseModel):
    version: str
    created_at: str = Field(default_factory=_now)
    status: Literal["active", "superseded", "quarantined"] = "active"
    healed_from: Optional[str] = None
    heal_reason: Optional[str] = None
    steps: list[PlaybookStep] = Field(default_factory=list)
    inputs: list[InputField] = Field(default_factory=list)
    source_run_id: Optional[str] = None

    @property
    def step_count(self) -> int:
        return len(self.steps)


class Connector(BaseModel):
    """One GUI-only system, compiled. Published to the registry, called by agents."""

    id: str
    portal: str
    operation: str
    base_url: str
    method: Literal["POST", "GET"] = "POST"
    owner: str = "sa-connector-default@clickwright.iam"
    requires_approval: bool = False
    # Navigation outside this set is refused, so a redirect or a poisoned link
    # cannot walk the browser somewhere the operator never authorised.
    allowed_hosts: list[str] = Field(default_factory=list)
    versions: list[ConnectorVersion] = Field(default_factory=list)

    @property
    def path(self) -> str:
        return f"/connectors/{self.id}/{self.operation}"

    def active(self) -> Optional[ConnectorVersion]:
        return next((v for v in self.versions if v.status == "active"), None)

    def get(self, version: str) -> Optional[ConnectorVersion]:
        return next((v for v in self.versions if v.version == version), None)

    def bump(self, kind: Literal["major", "minor", "patch"] = "minor") -> str:
        if not self.versions:
            return "1.0.0"
        major, minor, patch = (int(p) for p in self.versions[0].version.split("."))
        if kind == "major":
            return f"{major + 1}.0.0"
        if kind == "minor":
            return f"{major}.{minor + 1}.0"
        return f"{major}.{minor}.{patch + 1}"


# ── execution ────────────────────────────────────────────────────────────


class RunRecord(BaseModel):
    id: str
    connector_id: str
    mode: Literal["explore", "replay", "heal", "canary"]
    version: Optional[str] = None
    started_at: str = Field(default_factory=_now)
    duration_ms: int = 0
    model_cost_usd: float = 0.0
    status: Literal["ok", "failed", "held_for_approval"] = "ok"
    failed_step: Optional[int] = None
    error: Optional[str] = None
    result: dict[str, Any] = Field(default_factory=dict)
    policy_events: list["PolicyEvent"] = Field(default_factory=list)
    steps: list[TrajectoryStep] = Field(default_factory=list)


class PolicyEvent(BaseModel):
    kind: Literal["injection", "safety", "redaction"]
    at_step: Optional[int] = None
    detail: str
    action_taken: Literal["flagged", "blocked", "held", "redacted"] = "flagged"
    at: str = Field(default_factory=_now)


class ApprovalRequest(BaseModel):
    id: str
    run_id: str
    connector_id: str
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["pending", "approved", "denied"] = "pending"
    created_at: str = Field(default_factory=_now)
    # "invoke" is held before a replay starts; "in_run" is an agent paused
    # mid-exploration with its finger over the button; "input_needed" is an
    # agent asking for something only a person has — a code, a choice, a detail
    kind: Literal["invoke", "in_run", "input_needed"] = "invoke"
    action: Optional[str] = None  # what it is about to do, in the model's words
    target: Optional[str] = None  # the control it is about to operate
    screenshot: Optional[str] = None  # the screen it is looking at


class Benchmark(BaseModel):
    connector_id: str
    explore_ms: int
    explore_usd: float
    replay_ms: int
    replay_usd: float = 0.0
    measured_at: str = Field(default_factory=_now)

    @property
    def speedup(self) -> float:
        return self.explore_ms / self.replay_ms if self.replay_ms else 0.0


RunRecord.model_rebuild()
