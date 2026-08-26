"""Explorer for OpenAI-compatible endpoints — OpenRouter, a gateway, a proxy.

The Gemini path in `explorer.py` uses ADK's native computer-use tool, where the
action vocabulary and the screenshot round-trip are the model provider's. No
other provider offers that, so this backend owns the loop: it declares the
actions as ordinary function tools, executes them against the same
PlaywrightComputer, and hands the resulting screen back as an image.

Everything outside the loop is shared with the Gemini path — the same recorder,
the same host scoping, the same credential placeholders, the same redaction — so
a trajectory compiles identically whichever model produced it.

Selected by CLICKWRIGHT_MODEL_BASE_URL. See `build_explorer`.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from app.agents.explorer import INSTRUCTION, LIVE_SCREENSHOTS, MAX_STEPS
from app.computer import hosts
from app.computer.playwright_computer import PlaywrightComputer
from app.connectors.models import Trajectory, TrajectoryStep
from app.governance.gate import ApprovalGate
from app.governance.policy import PolicyGateway
from app.governance.redact import redact_step
from app.governance.secrets import resolve_credentials

MODEL = os.getenv("CLICKWRIGHT_EXPLORER_MODEL", "stealth/ox-alpha")
BASE_URL = os.getenv("CLICKWRIGHT_MODEL_BASE_URL", "")
API_KEY_VARS = ("CLICKWRIGHT_MODEL_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY")

# The model reads a screenshot and answers in the pixel coordinates of that
# screenshot — there is no virtual coordinate space to normalise from.
COORDINATE_NOTE = (
    "Coordinates are pixels in the screenshot you were last shown, measured from "
    "its top-left corner."
)

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": "Load a URL in the browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "intent": {"type": "string", "description": "Why, in a few words."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": (
                "Click a point. Clicking a dropdown returns its option labels; "
                "choose one by typing the label, never by clicking the open list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "intent": {"type": "string"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": "Type into the field that was last clicked.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "press_enter": {"type": "boolean"},
                    "clear_before_typing": {"type": "boolean"},
                    "intent": {"type": "string"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "key_press",
            "description": 'Press keys together, e.g. ["Control", "a"].',
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "array", "items": {"type": "string"}},
                    "intent": {"type": "string"},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the page from a point.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer"},
                    "y": {"type": "integer"},
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "magnitude": {"type": "integer"},
                    "intent": {"type": "string"},
                },
                "required": ["x", "y", "direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_back",
            "description": "Go back one page in history.",
            "parameters": {"type": "object", "properties": {"intent": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_operator",
            "description": (
                "Ask the person who started this run anything you cannot work out "
                "yourself: a one-time code sent to their phone or authenticator, a "
                "choice between options only they can make, a detail the task did "
                "not give you. They answer in the console and the run continues. "
                "Never guess where you could ask, and never ask for a password — "
                "those are already provided as {{password}}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "What to ask, e.g. 'Enter the 6-digit code just sent to your phone'.",
                    },
                    "sensitive": {
                        "type": "boolean",
                        "description": (
                            "True for a value you should type but never see — a code, "
                            "a PIN. You get a {{token}} to type instead. False for an "
                            "answer you need to reason about, which is returned as text."
                        ),
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Look at the current screen without acting on it.",
            "parameters": {"type": "object", "properties": {"intent": {"type": "string"}}},
        },
    },
]


def api_key() -> Optional[str]:
    for name in API_KEY_VARS:
        value = os.getenv(name)
        if value:
            return value
    return None


class OpenAIExplorer:
    """Same contract as Explorer, driven over chat completions."""

    def __init__(
        self,
        artifacts_dir: Optional[Path] = None,
        headless: bool = True,
        gate: Optional[ApprovalGate] = None,
    ):
        self.artifacts_dir = artifacts_dir
        self.headless = headless
        self.gateway = PolicyGateway()
        # None means nothing pauses the run: correct for a canary or a test,
        # wrong for an agent pointed at a system that can spend money
        self.gate = gate
        self._run_id = ""
        self._connector_id = ""
        self._answers = 0

    def _client(self):
        from openai import AsyncOpenAI  # optional dependency of the Gemini path

        key = api_key()
        if not key:
            raise RuntimeError(
                "No API key for the OpenAI-compatible endpoint. Set one of: "
                + ", ".join(API_KEY_VARS)
            )
        # One step is one request, and free tiers are rated per minute, so a run
        # that does not back off dies half-finished. The SDK's own retry covers
        # 429 and 5xx with exponential backoff.
        return AsyncOpenAI(
            api_key=key,
            base_url=BASE_URL or None,
            max_retries=int(os.getenv("CLICKWRIGHT_MODEL_RETRIES", "8")),
            timeout=120,
        )

    async def _act(self, computer: PlaywrightComputer, name: str, args: dict) -> dict:
        """Run one action, and answer even when it fails.

        A raised exception here ends the run and throws away every step taken so
        far. Handing the model the error and the current screen lets it try
        something else — a mistyped key is not a reason to lose the work.
        """
        try:
            held = await self._held(computer, name, args)
            if held:
                return held
            return await self._perform(computer, name, args)
        except hosts.HostRefused:
            raise  # a scope breach is not something to recover from
        except Exception as exc:
            computer.next_intent = None
            state = await computer.current_state()
            return {
                "url": state.url,
                "error": f"{name} failed: {type(exc).__name__}: {exc}",
                "screenshot": state.screenshot,
            }

    async def _held(self, computer: PlaywrightComputer, name: str, args: dict) -> Optional[dict]:
        """Pause on an irreversible action until a human decides. None means go.

        Only actions that operate a control can commit anything, so looking and
        scrolling are never held — an approval queue that fills with "it took a
        screenshot" is one nobody reads.
        """
        if self.gate is None or name not in ("click", "type", "type_text", "key_press"):
            return None

        target = ""
        if name == "click":
            selector = await computer._resolve_at(args.get("x", 0), args.get("y", 0))
            target = (selector.accessible_name or selector.text or selector.primary) if selector else ""

        allowed, reason = await self.gate.check(
            run_id=self._run_id,
            connector_id=self._connector_id,
            intent=str(args.get("intent") or ""),
            target=target,
            screenshot=computer.steps[-1].screenshot if computer.steps else None,
        )
        if allowed:
            return None

        state = await computer.current_state()
        return {
            "url": state.url,
            "error": f"refused by the operator: {reason}",
            "note": (
                "A human declined this action. Do not retry it. Either finish "
                "without it or stop and report what is left undone."
            ),
            "screenshot": state.screenshot,
        }

    async def _ask_operator(
        self, computer: PlaywrightComputer, question: str, sensitive: bool = True
    ) -> dict:
        """Put any question to the operator: a code, a choice, a missing detail.

        A sensitive answer comes back as a token rather than a value. The agent
        types `{{answer_1}}` and the browser substitutes it, so a one-time code
        never enters model context, the screenshots it sees next, or the
        recorded trajectory. A non-sensitive answer — which of these three
        addresses, what reference to use — is handed over as text, because the
        agent has to reason about it rather than just type it.
        """
        state = await computer.current_state()
        if self.gate is None:
            return {
                "url": state.url,
                "error": "There is no operator attached to this run; nobody can answer.",
                "screenshot": state.screenshot,
            }

        value = await self.gate.ask(
            run_id=self._run_id,
            connector_id=self._connector_id,
            question=question,
            screenshot=computer.steps[-1].screenshot if computer.steps else None,
        )
        if not value:
            return {
                "url": state.url,
                "error": "The operator did not answer. Stop and report what is left undone.",
                "screenshot": state.screenshot,
            }

        if not sensitive:
            return {
                "url": state.url,
                "answer": value,
                "screenshot": state.screenshot,
            }

        self._answers += 1
        token = f"answer_{self._answers}"
        computer.remember_secret(token, value)
        return {
            "url": state.url,
            "answered": True,
            "note": (
                f"The operator supplied it. Type the literal token {{{{{token}}}}} "
                "into the field — the browser substitutes the real value. You will "
                "not see it, and it is not in your screenshots."
            ),
            "screenshot": state.screenshot,
        }

    async def _perform(self, computer: PlaywrightComputer, name: str, args: dict) -> dict:
        computer.next_intent = args.get("intent") or None
        options: list[str] = []

        if name == "ask_operator":
            return await self._ask_operator(
                computer,
                str(args.get("question") or ""),
                sensitive=bool(args.get("sensitive", True)),
            )

        if name == "navigate":
            state = await computer.navigate(args["url"])
        elif name == "click":
            options = await computer.options_at(args["x"], args["y"])
            state = await computer.click_at(args["x"], args["y"])
        elif name in ("type", "type_text"):
            state = await computer.type_into_focus(
                args["text"],
                bool(args.get("press_enter", False)),
                bool(args.get("clear_before_typing", True)),
            )
        elif name == "key_press":
            state = await computer.key_combination(list(args.get("keys") or []))
        elif name == "scroll":
            state = await computer.scroll_at(
                args["x"], args["y"], args.get("direction", "down"), int(args.get("magnitude", 400))
            )
        elif name == "go_back":
            state = await computer.go_back()
        elif name == "take_screenshot":
            state = await computer.current_state()
        else:
            computer.next_intent = None
            state = await computer.current_state()
            return {"url": state.url, "error": f"No such action: {name}.", "screenshot": state.screenshot}

        result: dict[str, Any] = {"url": state.url, "screenshot": state.screenshot}
        if options:
            result["select_options"] = options
            result["note"] = (
                "This is a dropdown. Its open list is drawn by the operating system "
                "and is never in a screenshot, so clicking an option does nothing. "
                "Choose one by calling type with the option's exact label."
            )
        return result

    async def explore(
        self,
        connector_id: str,
        goal: str,
        start_url: str,
        allowed_hosts: Optional[list[str]] = None,
        on_step: Optional[Callable[[str, TrajectoryStep], None]] = None,
    ) -> Trajectory:
        run_id = f"run_{uuid.uuid4().hex[:6]}"
        self._run_id, self._connector_id = run_id, connector_id
        artifacts = self.artifacts_dir / run_id if self.artifacts_dir else None
        scope = hosts.normalise(allowed_hosts, start_url)
        hosts.check(start_url, scope)

        secrets = resolve_credentials(connector_id)
        computer = PlaywrightComputer(
            headless=self.headless,
            artifacts_dir=artifacts,
            allowed_hosts=scope,
            secrets=secrets,
        )
        await computer.initialize()

        client = self._client()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": f"{INSTRUCTION}\n{COORDINATE_NOTE}"},
            {"role": "user", "content": _task(goal, start_url, bool(secrets))},
        ]

        started = time.monotonic()
        final_text = ""
        published = 0

        try:
            while len(computer.steps) < MAX_STEPS:
                completion = await client.chat.completions.create(
                    model=MODEL, messages=_trimmed(messages), tools=TOOLS
                )
                choice = completion.choices[0].message
                calls = choice.tool_calls or []
                messages.append(
                    {
                        "role": "assistant",
                        "content": choice.content or "",
                        "tool_calls": [
                            {
                                "id": c.id,
                                "type": "function",
                                "function": {
                                    "name": c.function.name,
                                    "arguments": c.function.arguments,
                                },
                            }
                            for c in calls
                        ],
                    }
                    if calls
                    else {"role": "assistant", "content": choice.content or ""}
                )

                if not calls:
                    final_text = choice.content or ""
                    break

                for call in calls:
                    args = _arguments(call.function.arguments)
                    result = await self._act(computer, call.function.name, args)
                    messages.extend(_tool_reply(call.id, result))
                    published = self._publish(computer, run_id, published, on_step)
        finally:
            self._publish(computer, run_id, published, on_step)
            await computer.close()

        trajectory = Trajectory(
            run_id=run_id,
            connector_id=connector_id,
            goal=goal,
            model=MODEL,
            duration_ms=int((time.monotonic() - started) * 1000),
            steps=computer.steps,
        )
        for step in trajectory.steps:
            redact_step(step)

        injection = self.gateway.scan_text(final_text)
        if injection and trajectory.steps:
            trajectory.steps[-1].reason = (
                f"{trajectory.steps[-1].reason or ''} "
                f"[flagged: page contained instruction-style text {injection!r}]"
            ).strip()

        return trajectory

    def _publish(self, computer, run_id, published, on_step) -> int:
        if on_step is None:
            return len(computer.steps)
        for step in computer.steps[published:]:
            redact_step(step)
            on_step(run_id, step)
        return len(computer.steps)


def _task(goal: str, start_url: str, has_secrets: bool) -> str:
    task = f"Start at {start_url}\n\nGoal: {goal}"
    if has_secrets:
        task += (
            "\n\nCredentials: this operator is already provisioned. Type the literal "
            "token {{username}} into the user field and {{password}} into the password "
            "field — the browser replaces them with the real values. Any username you "
            "invent will be rejected by the site."
        )
    return task


def _arguments(raw: str) -> dict:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def _tool_reply(call_id: str, result: dict) -> list[dict[str, Any]]:
    """A tool result, then the screen it produced.

    Chat completions carry images on user messages, not tool messages, so the
    screenshot follows the result rather than travelling inside it.
    """
    screenshot = result.pop("screenshot", None)
    reply: list[dict[str, Any]] = [
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)}
    ]
    if screenshot:
        reply.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,"
                            + base64.b64encode(screenshot).decode()
                        },
                    }
                ],
            }
        )
    return reply


def _trimmed(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Send only the newest frames: every past screen would be resent otherwise.

    A screenshot costs roughly a thousand input tokens, and the conversation is
    replayed in full on every turn, so an untrimmed twenty-step run pays for two
    hundred screenshots it cannot act on.
    """
    frames = [i for i, m in enumerate(messages) if _is_frame(m)]
    stale = set(frames[: max(0, len(frames) - LIVE_SCREENSHOTS)])
    return [
        {"role": "user", "content": "[earlier screenshot omitted]"} if i in stale else m
        for i, m in enumerate(messages)
    ]


def _is_frame(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        part.get("type") == "image_url" for part in content
    )
