"""BaseComputer implementation for ADK's ComputerUseToolset.

ADK ships the toolset, not the driver — every method below becomes a tool the
model can call. The recorder hangs off the coordinate actions: before each one
dispatches, we resolve what is under the pointer so the trajectory contains
something the distiller can compile.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Literal, Optional

from google.adk.tools.computer_use.base_computer import (
    BaseComputer,
    ComputerEnvironment,
    ComputerState,
)
from playwright.async_api import Browser, Page, async_playwright

from app.computer import hosts
from app.computer.selectors import RESOLVE_JS, to_selector
from app.connectors.models import Action, Selector, TrajectoryStep


class PlaywrightComputer(BaseComputer):
    """Drives a real Chromium against a real server-rendered application."""

    def __init__(
        self,
        screen_size: tuple[int, int] = (1280, 936),
        headless: bool = True,
        artifacts_dir: Optional[Path] = None,
        allowed_hosts: Optional[list[str]] = None,
        secrets: Optional[dict[str, str]] = None,
    ):
        self._screen_size = screen_size
        self._headless = headless
        self._artifacts = artifacts_dir
        self._allowed_hosts = allowed_hosts or []
        # The model types "{{password}}"; the browser types the real value. The
        # secret is never in a prompt, a screenshot of a filled field, or a
        # recorded trajectory.
        self._secrets = secrets or {}
        self._pw = None
        self._browser: Optional[Browser] = None
        self._page: Optional[Page] = None
        self._t0 = time.monotonic()

        # what the recorder collects
        self.steps: list[TrajectoryStep] = []
        # the model states its intent per action; set just before the action runs
        # so the reason lands on the step it belongs to rather than a neighbour
        self.next_intent: Optional[str] = None
        self._last_at = time.monotonic()
        # set by _record, consumed by the _settled that follows it — page text is
        # captured after the dust settles, not while the click is still landing
        self._awaiting_snapshot = False

    # ── lifecycle ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        if self._page:
            return
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        width, height = self._screen_size
        context = await self._browser.new_context(viewport={"width": width, "height": height})
        self._page = await context.new_page()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._page = self._pw = None

    async def screen_size(self) -> tuple[int, int]:
        return self._screen_size

    async def environment(self) -> ComputerEnvironment:
        return ComputerEnvironment.ENVIRONMENT_BROWSER

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("PlaywrightComputer.initialize() has not been awaited")
        return self._page

    # ── recorder ─────────────────────────────────────────────────────────

    async def _resolve_at(self, x: int, y: int) -> Optional[Selector]:
        try:
            return to_selector(await self.page.evaluate(RESOLVE_JS, {"x": x, "y": y}))
        except Exception:
            return None

    async def _record(
        self,
        action: Action,
        *,
        selector: Optional[Selector] = None,
        value: Optional[str] = None,
        url: Optional[str] = None,
        submits: bool = False,
    ) -> None:
        now = time.monotonic()
        index = len(self.steps) + 1
        shot = None
        if self._artifacts:
            self._artifacts.mkdir(parents=True, exist_ok=True)
            shot = str(self._artifacts / f"step-{index:02d}.png")
            if not await self._screenshot(path=shot):
                shot = None  # the console shows the previous frame rather than a broken image
        self.steps.append(
            TrajectoryStep(
                index=index,
                action=action,
                value=value,
                url=url or self.page.url,
                selector=selector,
                submits=submits,
                reason=self.next_intent,
                ms=int((now - self._last_at) * 1000),
                screenshot=shot,
            )
        )
        self.next_intent = None
        self._last_at = now
        self._awaiting_snapshot = True

    async def _snapshot_text(self) -> str:
        """The page's visible text, whitespace-normalised, for assertion checking.

        A capped snapshot is all the distiller needs to tell a real assertion
        from an invented one; the full document would only bloat every store
        the trajectory ends up in.
        """
        try:
            raw = await self.page.inner_text("body")
        except Exception:  # noqa: BLE001 - mid-navigation reads fail; that is fine
            return ""
        return " ".join(raw.split())[:4000]

    def remember_secret(self, name: str, value: str) -> None:
        """Hold a value the operator supplied mid-run — a code, a PIN, an answer.

        It joins the credentials table, so the agent types `{{name}}` and the
        browser substitutes it. The value stays out of model context and out of
        the recorded step, exactly like a password.
        """
        self._secrets[name.strip().lower()] = value

    def attach_reason(self, index: int, reason: str) -> None:
        """Called by the explorer's after_model_callback once the model's stated
        intent for a step is known (ADK does not surface it to the tool layer)."""
        for step in self.steps:
            if step.index == index:
                step.reason = reason
                return

    # ── state ────────────────────────────────────────────────────────────

    async def current_state(self) -> ComputerState:
        return ComputerState(screenshot=await self._screenshot(), url=self.page.url)

    async def _screenshot(self, path: Optional[str] = None) -> bytes:
        """Chromium refuses to capture while a navigation is committing.

        The click that submits a form is exactly when that happens, so a run
        would die on its last action. Wait for the new document and try again;
        a frame is worth losing, the run is not.
        """
        for attempt in (1, 2, 3):
            try:
                return await self.page.screenshot(path=path) if path else await self.page.screenshot()
            except Exception:
                if attempt == 3:
                    return b""
                try:
                    await self.page.wait_for_load_state("load", timeout=3000)
                except Exception:
                    pass
        return b""

    async def _settled(self) -> ComputerState:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:
            pass
        # A click can navigate too. Check where we actually landed, not just
        # where we were asked to go.
        if self._allowed_hosts and self.page.url.startswith(("http://", "https://")):
            try:
                hosts.check(self.page.url, self._allowed_hosts)
            except hosts.HostRefused:
                await self.page.go_back()
                raise
        if self._awaiting_snapshot and self.steps:
            self.steps[-1].page_text = await self._snapshot_text()
            self._awaiting_snapshot = False
        return await self.current_state()

    # ── navigation ───────────────────────────────────────────────────────

    async def open_web_browser(self) -> ComputerState:
        await self.initialize()
        return await self.current_state()

    async def navigate(self, url: str) -> ComputerState:
        hosts.check(url, self._allowed_hosts)
        await self.page.goto(url, wait_until="domcontentloaded")
        await self._record(Action.NAVIGATE, value=url, url=url)
        return await self._settled()

    async def go_back(self) -> ComputerState:
        await self.page.go_back()
        return await self._settled()

    async def go_forward(self) -> ComputerState:
        await self.page.go_forward()
        return await self._settled()

    async def search(self) -> ComputerState:
        # Leaving the target to search would take the agent out of scope, and
        # nothing about the task requires it.
        raise hosts.HostRefused("search is disabled: the agent stays on the target")

    # ── pointer ──────────────────────────────────────────────────────────

    async def click_at(self, x: int, y: int) -> ComputerState:
        selector = await self._resolve_at(x, y)
        await self.page.mouse.click(x, y)
        await self._record(Action.CLICK, selector=selector)
        return await self._settled()

    async def options_at(self, x: int, y: int) -> list[str]:
        """The choices of a <select> under this point, or [] for anything else.

        Chromium renders a native dropdown outside the page, so it never appears
        in a screenshot: a model that clicks one sees the page unchanged, clicks
        where it believes the options are, and repeats until the run is out of
        quota. Handing it the labels ends that.
        """
        try:
            return await self.page.evaluate(
                "({x, y}) => { const e = document.elementFromPoint(x, y);"
                " return e && e.tagName === 'SELECT'"
                "   ? [...e.options].map(o => o.label || o.text) : []; }",
                {"x": x, "y": y},
            )
        except Exception:
            return []

    async def hover_at(self, x: int, y: int) -> ComputerState:
        await self.page.mouse.move(x, y)
        return await self.current_state()

    async def type_text_at(
        self,
        x: int,
        y: int,
        text: str,
        press_enter: bool = True,
        clear_before_typing: bool = True,
    ) -> ComputerState:
        selector = await self._resolve_at(x, y)
        await self.page.mouse.click(x, y)
        return await self._type(selector, text, press_enter, clear_before_typing)

    async def type_into_focus(
        self,
        text: str,
        press_enter: bool = False,
        clear_before_typing: bool = True,
    ) -> ComputerState:
        """Type where the caret already is.

        Gemini 3.5's `type` action carries no coordinates — it clicks a field
        first and then types into it, so there is nothing to resolve a selector
        from except the focused element.
        """
        return await self._type(
            await self._focused_selector(), text, press_enter, clear_before_typing
        )

    async def _focused_selector(self) -> Optional[Selector]:
        try:
            box = await self.page.evaluate(
                "() => { const e = document.activeElement;"
                " if (!e || e === document.body) return null;"
                " const r = e.getBoundingClientRect();"
                " return {x: r.left + r.width / 2, y: r.top + r.height / 2}; }"
            )
        except Exception:
            return None
        return await self._resolve_at(box["x"], box["y"]) if box else None

    async def _type(
        self,
        selector: Optional[Selector],
        text: str,
        press_enter: bool,
        clear_before_typing: bool,
    ) -> ComputerState:
        placeholder, typed = self._resolve_placeholder(text)

        if clear_before_typing:
            await self.page.keyboard.press("ControlOrMeta+a")
            await self.page.keyboard.press("Delete")
        await self.page.keyboard.type(typed, delay=12)

        # a <select> is driven by value, not keystrokes — record it as such
        action = Action.TYPE
        if selector and selector.primary and await self._is_select(selector.primary):
            await self.page.select_option(selector.primary, label=typed)
            action = Action.SELECT

        if press_enter:
            await self.page.keyboard.press("Enter")
        # record the placeholder, never the substituted value. `submits` is the
        # difference between filling a search box and searching.
        await self._record(action, selector=selector, value=placeholder or typed, submits=press_enter)
        return await self._settled()

    def _resolve_placeholder(self, text: str) -> tuple[Optional[str], str]:
        """Map "{{password}}" to the stored value, keeping the token for the record.

        Returns (placeholder_or_None, text_to_type).
        """
        stripped = (text or "").strip()
        if stripped.startswith("{{") and stripped.endswith("}}"):
            name = stripped[2:-2].strip().lower()
            if name in self._secrets:
                return stripped, self._secrets[name]
        return None, text

    async def _is_select(self, selector: str) -> bool:
        try:
            return await self.page.locator(selector).first.evaluate("e => e.tagName === 'SELECT'")
        except Exception:
            return False

    async def drag_and_drop(
        self, x: int, y: int, destination_x: int, destination_y: int
    ) -> ComputerState:
        await self.page.mouse.move(x, y)
        await self.page.mouse.down()
        await self.page.mouse.move(destination_x, destination_y, steps=12)
        await self.page.mouse.up()
        return await self._settled()

    async def scroll_at(
        self,
        x: int,
        y: int,
        direction: Literal["up", "down", "left", "right"],
        magnitude: int,
    ) -> ComputerState:
        dx = magnitude if direction == "right" else -magnitude if direction == "left" else 0
        dy = magnitude if direction == "down" else -magnitude if direction == "up" else 0
        await self.page.mouse.move(x, y)
        await self.page.mouse.wheel(dx, dy)
        return await self.current_state()

    async def scroll_document(
        self, direction: Literal["up", "down", "left", "right"]
    ) -> ComputerState:
        key = {"up": "PageUp", "down": "PageDown", "left": "Home", "right": "End"}[direction]
        await self.page.keyboard.press(key)
        return await self.current_state()

    # ── keyboard / timing ────────────────────────────────────────────────

    # Models name keys the way people do. Playwright wants the DOM key values.
    KEY_ALIASES = {
        "left": "ArrowLeft", "right": "ArrowRight", "up": "ArrowUp", "down": "ArrowDown",
        "esc": "Escape", "del": "Delete", "return": "Enter", "space": " ",
        "ctrl": "Control", "cmd": "Meta", "command": "Meta", "option": "Alt",
        "pgup": "PageUp", "pgdn": "PageDown", "pagedown": "PageDown",
    }

    async def key_combination(self, keys: list[str]) -> ComputerState:
        resolved = [self.KEY_ALIASES.get(k.strip().lower(), k.strip()) for k in keys if k]
        await self.page.keyboard.press("+".join(resolved))
        return await self._settled()

    async def wait(self, seconds: int) -> ComputerState:
        await self.page.wait_for_timeout(min(seconds, 10) * 1000)
        return await self.current_state()

    # ── helpers ──────────────────────────────────────────────────────────

    async def screenshot_b64(self) -> str:
        return base64.b64encode(await self.page.screenshot()).decode()
