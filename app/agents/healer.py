"""Healer — keeps compiled connectors true as the systems underneath them change.

Runs unattended on a schedule. Replays each active connector against the live
system with a canary payload; if a step stops matching, escalates back to
computer use, recompiles, and publishes the next version. No human is asked.

Two things a failing canary can mean, and they need opposite responses:

  * the page moved      — a playbook problem; re-learn it (this file's job)
  * the target is down  — DNS, connection refused, a portal that is switched
                          off. No model run can fix that, so none is started.

This is the failure path the rubric pays for, and it is the reason a compiled
artifact is worth more than a recorded macro.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from app.agents.distiller import Distiller
from app.agents.backend import build_explorer
from app.agents.explorer import Explorer
from app.connectors.models import Connector, ConnectorVersion, RunRecord
from app.connectors.registry import Registry
from app.connectors.runtime import ConnectorRuntime

CANARY_PREFIX = os.getenv("CLICKWRIGHT_CANARY_PREFIX", "CANARY")

HealStrategy = Literal["step", "full", "auto"]

# Transport-level Chromium failures. A page that never loaded says nothing
# about whether the playbook still fits it.
_UNREACHABLE = re.compile(
    r"net::ERR_(?:NAME_NOT_RESOLVED|INTERNET_DISCONNECTED|ADDRESS_UNREACHABLE"
    r"|CONNECTION_(?:REFUSED|RESET|CLOSED|TIMED_OUT)|TUNNEL_CONNECTION_FAILED"
    r"|PROXY_CONNECTION_FAILED|EMPTY_RESPONSE)"
    r"|NS_ERROR_UNKNOWN_HOST",
    re.IGNORECASE,
)


def heal_strategy() -> HealStrategy:
    """step patches the broken step; full rebuilds the playbook from a fresh
    traversal; auto patches once and escalates to full if an already-healed
    version fails again."""
    value = (os.getenv("CLICKWRIGHT_HEAL_STRATEGY") or "auto").strip().lower()
    return value if value in ("step", "full", "auto") else "auto"


def _short(text: str, limit: int = 90) -> str:
    """One line a person can read in a status pill."""
    collapsed = " ".join((text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


@dataclass
class HealResult:
    connector_id: str
    healthy: bool
    run: RunRecord
    healed_to: Optional[str] = None
    reason: Optional[str] = None
    # False means "not something healing can fix" — the target was unreachable,
    # so escalating to the model would burn quota to discover that again.
    healable: bool = True


class Healer:
    def __init__(
        self,
        registry: Optional[Registry] = None,
        runtime: Optional[ConnectorRuntime] = None,
        explorer: Optional[Explorer] = None,
        distiller: Optional[Distiller] = None,
        artifacts_dir: Optional[Path] = None,
    ):
        self.registry = registry or Registry()
        self.runtime = runtime or ConnectorRuntime()
        self.explorer = explorer or build_explorer(artifacts_dir=artifacts_dir)
        self._distiller = distiller

    @property
    def distiller(self) -> Distiller:
        if self._distiller is None:
            self._distiller = Distiller()
        return self._distiller

    # ── canary ───────────────────────────────────────────────────────────

    @staticmethod
    def canary_inputs(version: ConnectorVersion) -> dict:
        """A payload the target will accept but that is obviously synthetic.

        An input carrying the value the recording actually used (backfilled by
        the distiller into `example`) replays realistically — a search box gets
        a query the site provably returns results for, not gibberish.
        """
        values: dict[str, object] = {}
        for field in version.inputs:
            if field.type == "number":
                values[field.name] = 1
            elif field.type == "boolean":
                values[field.name] = True
            else:
                values[field.name] = field.example or f"{CANARY_PREFIX}-{field.name.upper()}"
        return values

    @staticmethod
    def _unreachable(run: RunRecord) -> bool:
        """True when nothing executed and the browser never got a page.

        Any completed step means the target answered, so the failure belongs to
        the playbook. Zero progress plus a transport error belongs to the wire.
        """
        if run.steps:
            return False
        error = run.error or ""
        return bool(_UNREACHABLE.search(error))

    async def check(self, connector: Connector) -> HealResult:
        version = connector.active()
        if not version:
            raise ValueError(f"{connector.id} has no active version")

        run = await self.runtime.execute(
            connector,
            version,
            self.canary_inputs(version),
            mode="canary",
            approved=True,  # a canary is authorised by definition
        )

        if run.status != "failed":
            return HealResult(connector.id, healthy=True, run=run)

        if self._unreachable(run):
            return HealResult(
                connector.id,
                healthy=False,
                run=run,
                healable=False,
                reason=f"target unreachable — will retry on the next canary ({_short(run.error or '')})",
            )

        return HealResult(
            connector.id,
            healthy=False,
            run=run,
            reason=_short(run.error or "canary failed"),
        )

    # ── heal ─────────────────────────────────────────────────────────────

    async def heal(self, connector: Connector, result: HealResult) -> HealResult:
        """Escalate the failure back to the model and publish the next version."""
        version = connector.active()

        strategy = heal_strategy()
        # auto: one patch gets the benefit of the doubt. If a version that a
        # heal already produced fails again, the single-step theory is worn
        # out — rebuild the whole playbook from a fresh traversal instead.
        rebuild = strategy == "full" or (strategy == "auto" and version.healed_from is not None)

        if rebuild:
            trajectory = await self.explorer.explore(
                connector_id=connector.id,
                goal=self._rebuild_goal(connector, version),
                start_url=(version.steps[0].url if version.steps and version.steps[0].url else None)
                or connector.base_url,
                allowed_hosts=connector.allowed_hosts,
            )
            # The full compile calls its provider synchronously; off the event
            # loop, or a slow model freezes every endpoint in the process.
            next_version = await asyncio.to_thread(
                self.distiller.compile,
                trajectory,
                version=connector.bump("minor"),
                healed_from=version.version,
                heal_reason=result.reason or "canary failure",
            )
        else:
            failed_index = result.run.failed_step or 1
            failed_step = next((s for s in version.steps if s.index == failed_index), None)
            trajectory = await self.explorer.explore(
                connector_id=connector.id,
                goal=self._recovery_goal(connector, version, failed_step, result.reason or ""),
                start_url=(
                    result.run.result.get("final_url")
                    or (version.steps[0].url if version.steps and version.steps[0].url else None)
                    or connector.base_url
                ),
                allowed_hosts=connector.allowed_hosts,
            )
            next_version = self.distiller.recompile_step(
                previous=version,
                trajectory=trajectory,
                failed_index=failed_index,
                version=connector.bump("minor"),
                reason=result.reason or "canary failure",
            )

        # A publish that changes nothing would put a new number over an old bug
        # — and the next canary would fail identically, forever. Two guards:
        # against the snapshot this heal started from, and against the registry
        # head another concurrent heal may have advanced in the meantime.
        candidate_steps = [s.model_dump() for s in next_version.steps]
        if candidate_steps == [s.model_dump() for s in version.steps]:
            return self._nothing_to_publish(result, "recovery produced no change")
        head = self.registry.get(connector.id)
        head_active = head.active() if head else None
        if head_active and candidate_steps == [s.model_dump() for s in head_active.steps]:
            return self._nothing_to_publish(
                result, "an equivalent version is already published"
            )

        self.registry.publish(connector, next_version)

        return HealResult(
            connector.id,
            healthy=True,
            run=result.run,
            healed_to=next_version.version,
            reason=result.reason,
        )

    @staticmethod
    def _nothing_to_publish(result: HealResult, why: str) -> HealResult:
        original = _short(result.reason or "canary failure")
        return HealResult(
            result.connector_id,
            healthy=False,
            run=result.run,
            reason=(
                f"{why}; nothing published. Original failure: {original}"
            ),
        )

    @staticmethod
    def _recovery_goal(connector, version, failed_step, reason: str) -> str:
        described = (
            f"step {failed_step.index} ({failed_step.action.value}"
            + (f" on {failed_step.selector.primary}" if failed_step.selector else "")
            + ")"
            if failed_step
            else "a step"
        )
        return (
            f"A saved automation for {connector.portal} stopped working at {described}. "
            f"The failure was: {reason}. "
            f"The page has changed. Complete the same action the step was meant to perform, "
            f"then stop. Do not repeat earlier steps that already succeeded."
        )

    @staticmethod
    def _rebuild_goal(connector: Connector, version: ConnectorVersion) -> str:
        """The whole-task brief for a full rebuild. The original exploration
        goal is not stored on the connector, so it is reconstructed from what
        is: the portal, the operation, and the inputs callers must supply."""
        fields = ", ".join(field.name for field in version.inputs) or "none"
        return (
            f"Redo the saved automation for {connector.portal} end to end "
            f"({connector.operation.replace('-', ' ')}). "
            f"The caller supplies these inputs: {fields}. "
            f"Perform the complete task once, exactly as a user would, then stop."
        )

    # ── entry point for the scheduled job ────────────────────────────────

    async def sweep(self) -> list[HealResult]:
        results: list[HealResult] = []
        for connector in self.registry.list():
            if not connector.active():
                continue
            result = await self.check(connector)
            if not result.healthy and result.healable:
                result = await self.heal(connector, result)
            results.append(result)
        return results
