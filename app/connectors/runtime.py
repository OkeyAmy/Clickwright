"""Deterministic playbook execution — no model in the loop.

This is the fast path. Once a connector is compiled, calling it costs Cloud Run
compute and nothing else. When a selector stops matching, the runtime does not
guess: it fails with the step index so the healer can escalate that one step
back to computer use.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from app.computer import hosts
from app.connectors.models import (
    Action,
    Connector,
    ConnectorVersion,
    RunRecord,
    TrajectoryStep,
)
from app.governance.policy import PolicyGateway
from app.governance.secrets import resolve_credentials


class StepFailure(RuntimeError):
    def __init__(self, index: int, message: str):
        super().__init__(message)
        self.index = index


class ConnectorRuntime:
    """Executes a compiled playbook against the live target system."""

    def __init__(self, headless: bool = True, gateway: Optional[PolicyGateway] = None):
        self.headless = headless
        self.gateway = gateway or PolicyGateway()
        self._seen: dict[str, RunRecord] = {}  # idempotency keys

    async def execute(
        self,
        connector: Connector,
        version: ConnectorVersion,
        inputs: dict[str, Any],
        *,
        mode: str = "replay",
        idempotency_key: Optional[str] = None,
        approved: bool = False,
    ) -> RunRecord:
        if idempotency_key and idempotency_key in self._seen:
            return self._seen[idempotency_key]

        run = RunRecord(
            id=f"run_{uuid.uuid4().hex[:6]}",
            connector_id=connector.id,
            mode=mode,  # type: ignore[arg-type]
            version=version.version,
        )

        decision = self.gateway.evaluate(connector, inputs)
        run.policy_events.extend(decision.events)
        if decision.hold and not approved:
            run.status = "held_for_approval"
            run.result = {"reason": decision.reason}
            if idempotency_key:
                self._seen[idempotency_key] = run
            return run

        started = time.monotonic()
        try:
            run.result = await self._drive(connector, version, inputs, run)
        except StepFailure as exc:
            run.status = "failed"
            run.failed_step = exc.index
            run.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - surface anything the browser throws
            run.status = "failed"
            run.error = f"{type(exc).__name__}: {exc}"

        run.duration_ms = int((time.monotonic() - started) * 1000)
        if idempotency_key:
            self._seen[idempotency_key] = run
        return run

    # ── the actual driving ───────────────────────────────────────────────

    async def _drive(
        self,
        connector: Connector,
        version: ConnectorVersion,
        inputs: dict[str, Any],
        run: RunRecord,
    ) -> dict[str, Any]:
        credentials = resolve_credentials(connector.id)
        values = {**inputs, **credentials}  # credentials never enter model context

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            page = await (await browser.new_context(viewport={"width": 1280, "height": 936})).new_page()
            try:
                for step in version.steps:
                    at = time.monotonic()
                    try:
                        await self._step(page, step, values, connector)
                    except StepFailure:
                        # Where the run was when it stopped trusting the page.
                        # The healer starts there instead of from the top.
                        run.result = {"final_url": page.url}
                        raise
                    run.steps.append(
                        TrajectoryStep(
                            index=step.index,
                            action=step.action,
                            value=self._value(step, values, redact=True),
                            selector=step.selector,
                            url=page.url,
                            ms=int((time.monotonic() - at) * 1000),
                        )
                    )
                return {
                    "status": "ok",
                    "reference": await self._reference(page),
                    "final_url": page.url,
                    "confirmation": await self._confirmation(page),
                }
            finally:
                await browser.close()

    async def _step(self, page, step, values: dict[str, Any], connector: Connector) -> None:
        if step.action is Action.NAVIGATE:
            url = (step.url or step.value or "").replace("{base_url}", connector.base_url)
            try:
                hosts.check(url, connector.allowed_hosts)
            except hosts.HostRefused as exc:
                raise StepFailure(step.index, str(exc)) from exc
            await page.goto(url, wait_until="domcontentloaded")
            return

        if step.action is Action.WAIT:
            await page.wait_for_timeout(step.timeout_ms)
            return

        if step.action is Action.ASSERT:
            expected = self._expected(step, values)
            if expected and expected not in await page.inner_text("body"):
                raise StepFailure(step.index, f"assertion failed: expected {expected!r}")
            return

        locator = await self._locate(page, step)
        value = self._value(step, values)

        if step.action is Action.CLICK:
            await locator.click(timeout=step.timeout_ms)
        elif step.action is Action.TYPE:
            await locator.fill(str(value or ""), timeout=step.timeout_ms)
            if step.submits:
                # the recorded run pressed Enter here; without it a search box
                # gets filled and nothing is ever searched
                await locator.press("Enter", timeout=step.timeout_ms)
        elif step.action is Action.SELECT:
            await locator.select_option(label=str(value or ""), timeout=step.timeout_ms)

        try:
            await page.wait_for_load_state("networkidle", timeout=4000)
        except PlaywrightError:
            pass

        expected = self._expected(step, values)
        if expected and expected not in await page.inner_text("body"):
            raise StepFailure(step.index, f"assertion failed after {step.action.value}: expected {expected!r}")

    async def _locate(self, page, step):
        """Try each selector candidate in order. Failing all of them is the
        signal the healer listens for — never a silent best guess."""
        if not step.selector:
            raise StepFailure(step.index, "step has no selector")
        errors = []
        for candidate in step.selector.candidates():
            try:
                locator = page.locator(candidate).first
                await locator.wait_for(state="visible", timeout=1500)
                return locator
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{candidate}: {type(exc).__name__}")
        raise StepFailure(
            step.index,
            f"no selector matched ({len(errors)} candidates tried): {step.selector.primary}",
        )

    @staticmethod
    def _expected(step, values: dict[str, Any]) -> str:
        """The text this step should see, with `{{input}}` filled in.

        The distiller writes the assertion for a *class* of runs, so "the page
        now shows what you searched for" comes back as `{{search_query}}`.
        Compared literally it fails every replay but the recorded one.
        """
        text = step.expect_text or ""
        # written either as {{search_query}} or, just as often, as the bare
        # input name — both mean "whatever the caller passed"
        if text in values:
            return str(values[text])
        for name, value in values.items():
            text = text.replace(f"{{{{{name}}}}}", str(value))
        return text

    @staticmethod
    def _value(step, values: dict[str, Any], redact: bool = False) -> Any:
        if step.value_from:
            raw = values.get(step.value_from)
            if redact and step.value_from.endswith(("password", "pass", "secret")):
                return "••••••"
            if raw is None:
                return None
            # Callers send JSON: a number input arrives as an int, not a str.
            # The recorded trajectory is text, so stringify at the boundary.
            return raw if isinstance(raw, str) else str(raw)
        return step.value

    @staticmethod
    async def _confirmation(page) -> str:
        """What the system said back.

        Probing status containers first matters: the first line of <body> is
        usually chrome, which would report a masthead as the outcome.
        """
        for selector in ("[role=status]", ".ok", ".alert-success", ".confirmation", ".message"):
            try:
                node = page.locator(selector).first
                if await node.count():
                    return " ".join((await node.inner_text()).split())
            except Exception:  # noqa: BLE001
                continue
        body = (await page.inner_text("body")).strip()
        return next((line.strip() for line in body.splitlines() if line.strip()), "")

    @staticmethod
    async def _reference(page) -> Optional[str]:
        for selector in ("#ctl00_reference", "[data-reference]", ".reference"):
            try:
                node = page.locator(selector).first
                if await node.count():
                    return (await node.inner_text()).strip()
            except Exception:  # noqa: BLE001
                continue
        return None
