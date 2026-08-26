"""Distiller — turns one exploration into a reusable artifact.

The explorer's trajectory is a record of what happened once, with literal values
baked in. The distiller decides which of those values were *inputs* (and should
become request fields), which were incidental, and what each step should assert
so a future replay can tell success from a silently changed page.

Structured output, so the result is a schema-valid ConnectorVersion rather than
prose that has to be parsed.
"""

from __future__ import annotations

import json
import re
import os
from typing import Any

from google import genai
from google.genai import types

from app.connectors.models import (
    Action,
    ConnectorVersion,
    InputField,
    PlaybookStep,
    Selector,
    Trajectory,
    TrajectoryStep,
)

MODEL = os.getenv("CLICKWRIGHT_DISTILLER_MODEL", "gemini-3.5-flash")

PROMPT = """\
You are compiling a recorded browser session into a reusable connector.

Below is a trajectory: every action an agent took against a web application \
that has no API, in order, with the selector each action resolved to and the \
literal value it used.

Produce a playbook that reproduces this outcome for *different* inputs.

Rules:
- Any value that a caller would reasonably want to change becomes an input \
  field, referenced by `value_from`. Anything structural (a menu choice that is \
  always the same, a fixed URL) stays a literal in `value`.
- Credentials are never inputs and never literals. If a step typed into a \
  password or username field, set `value_from` to "username" or "password".
- Give every input a snake_case name, a type, and a one-line description an \
  agent could act on without seeing the page.
- Add an `expect_text` to the steps where the page visibly changes state \
  (after submission, after a stage transition). Use text that describes the \
  outcome, not decoration. Leave it null elsewhere.
- Keep the step order exactly as recorded. Do not add or remove steps.

Trajectory:
{trajectory}
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "inputs": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": ["string", "number", "boolean"]},
                    "required": {"type": "boolean"},
                    "description": {"type": "string"},
                    "example": {"type": "string"},
                },
                "required": ["name", "type", "required", "description"],
            },
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "action": {
                        "type": "string",
                        "enum": [a.value for a in Action],
                    },
                    "value_from": {"type": "string"},
                    "value": {"type": "string"},
                    "expect_text": {"type": "string"},
                },
                "required": ["index", "action"],
            },
        },
    },
    "required": ["inputs", "steps"],
}


def _json_object(text: str) -> dict:
    """Parse the playbook out of a chat reply.

    `response_format` is a request, not a guarantee. Observed from ox-alpha: the
    object arrives inside a ```json fence, wrapped under a "playbook" key, with
    a paragraph of commentary after the fence. All three are recoverable, and a
    compile that throws would discard a browser run that already succeeded.
    """
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1).strip()

    for attempt in (candidate, _balanced(candidate), _repaired(_balanced(candidate))):
        if not attempt:
            continue
        try:
            return _unwrapped(json.loads(attempt))
        except ValueError:
            continue

    raise MalformedPlaybook(text)


class MalformedPlaybook(ValueError):
    """The reply was not JSON we could repair. Carries the reply, for one retry."""

    def __init__(self, reply: str):
        self.reply = reply
        detail = json_error(reply)
        super().__init__(f"the model's reply is not usable JSON: {detail}")


def json_error(reply: str) -> str:
    """Where the JSON went wrong, and the text around it — the useful half of a log."""
    try:
        json.loads(_balanced(reply) or reply)
    except ValueError as exc:
        position = getattr(exc, "pos", 0)
        return f"{exc} — near {reply[max(0, position - 90) : position + 90]!r}"
    return "no error"


def _balanced(text: str) -> str:
    """The first complete JSON object, ignoring braces inside strings.

    Taking everything up to the last `}` breaks whenever the model adds a
    closing sentence — and this one does.
    """
    start = text.find("{")
    if start == -1:
        return ""
    depth, in_string, escaped = 0, False, False
    for i, char in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def _repaired(text: str) -> str:
    """The two malformations a model actually produces: trailing commas, and a
    raw newline inside a string."""
    if not text:
        return ""
    without_trailing_commas = re.sub(r",(\s*[}\]])", r"\1", text)
    return re.sub(
        r'"(?:[^"\\]|\\.)*"',
        lambda m: m.group(0).replace("\n", "\\n").replace("\r", ""),
        without_trailing_commas,
        flags=re.DOTALL,
    )


def _unwrapped(parsed: dict) -> dict:
    """Accept {"playbook": {...}} as readily as the bare object."""
    if "steps" in parsed:
        return parsed
    for value in parsed.values():
        if isinstance(value, dict) and "steps" in value:
            return value
    raise ValueError(f"the model's reply has no steps: keys were {sorted(parsed)}")


def _normalise(text: str | None) -> str:
    return " ".join((text or "").split())


_INPUT_TYPES = {"string", "number", "boolean"}


def _sanitise_inputs(fields: list[dict] | None) -> list[dict]:
    """Force whatever the model returned into valid InputFields.

    A schema-violating reply used to raise after the browser work had already
    succeeded — losing a whole exploration over one wrong enum. Names are
    slugified, unknown types fall back to string, everything becomes a str."""
    import re as _re

    seen: set[str] = set()
    out: list[dict] = []
    for field in fields or []:
        if not isinstance(field, dict):
            continue
        name = _re.sub(r"\W+", "_", str(field.get("name") or "")).strip("_").lower()
        base, n = name or "field", 2
        while name in seen:
            name, n = f"{base}_{n}", n + 1
        seen.add(name)
        clean: dict = {
            "name": name,
            "type": field.get("type") if field.get("type") in _INPUT_TYPES else "string",
            "required": bool(field.get("required", True)),
            "description": str(field.get("description") or "")[:300],
        }
        if field.get("example"):
            clean["example"] = str(field["example"])[:120]
        out.append(clean)
    return out


def _verified_text(expect_text: str | None, corpus: list[str | None]) -> str | None:
    """Keep an assertion only if the page it was recorded on actually said it.

    Models asked to "describe the outcome" produce prose like *Search results
    for the entered query are displayed* — text that exists in no rendering of
    any page. Compared literally by the runtime, such an assertion fails every
    replay forever. An empty corpus means we could not check either way, so the
    assertion stands.
    """
    needle = _normalise(expect_text)
    if not needle:
        return None
    haystack = _normalise(" ".join(t for t in corpus if t)).lower()
    if not haystack:
        return expect_text
    return expect_text if needle.lower() in haystack else None


class Distiller:
    def __init__(self, client: genai.Client | None = None):
        # A run explored over an OpenAI-compatible endpoint has no Gemini key to
        # compile with. Whichever provider drove the browser also compiles.
        self._openai_base = os.getenv("CLICKWRIGHT_MODEL_BASE_URL") if client is None else ""
        self.client = client or (None if self._openai_base else genai.Client())

    def _raw_compile(self, trajectory: Trajectory) -> dict:
        """The model call alone: a playbook-shaped dict, before the architecture
        verifies it. Exposed separately so benchmarks can measure how much of
        the model's output survives assembly."""
        payload = json.dumps(
            [
                {
                    "index": s.index,
                    "action": s.action.value,
                    "value": s.value,
                    "url": s.url,
                    "selector": s.selector.primary if s.selector else None,
                    "accessible_name": s.selector.accessible_name if s.selector else None,
                    "reason": s.reason,
                }
                for s in trajectory.steps
            ],
            indent=2,
        )
        prompt = PROMPT.format(trajectory=payload)
        if self._openai_base:
            return self._compile_over_openai(prompt)
        response = self.client.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
                temperature=0.0,
            ),
        )
        return json.loads(response.text)

    def compile(self, trajectory: Trajectory, version: str, healed_from: str | None = None,
                heal_reason: str | None = None) -> ConnectorVersion:
        compiled = self._raw_compile(trajectory)
        return self._assemble(trajectory, compiled, version, healed_from, heal_reason)

    def _compile_over_openai(self, prompt: str) -> dict:
        """Same job, chat-completions shape: a JSON schema instead of a response_schema."""
        from openai import OpenAI

        from app.agents.openai_explorer import BASE_URL, MODEL as OPENAI_MODEL, api_key

        client = OpenAI(api_key=api_key(), base_url=BASE_URL, max_retries=4, timeout=120)
        messages: list[dict[str, str]] = [{"role": "user", "content": prompt}]
        for attempt in (1, 2):
            reply = self._ask(client, messages)
            try:
                return _json_object(reply)
            except MalformedPlaybook as broken:
                if attempt == 2:
                    raise
                # Cheaper than throwing away a browser run that already worked.
                messages += [
                    {"role": "assistant", "content": broken.reply},
                    {
                        "role": "user",
                        "content": (
                            f"That reply could not be parsed: {json_error(broken.reply)}. "
                            "Send the same playbook again as one valid JSON object and "
                            "nothing else — no prose, no code fence. Escape every quote "
                            "and newline inside a string."
                        ),
                    },
                ]
        raise AssertionError("unreachable")

    def _ask(self, client, messages: list[dict[str, str]]) -> str:
        from app.agents.openai_explorer import MODEL as OPENAI_MODEL

        completion = client.chat.completions.create(
            # CLICKWRIGHT_DISTILLER_MODEL names a Gemini model; an OpenAI-
            # compatible endpoint would reject it. Same model as the explorer
            # unless this endpoint is given one of its own.
            model=os.getenv("CLICKWRIGHT_OPENAI_DISTILLER_MODEL") or OPENAI_MODEL,
            messages=messages,
            temperature=0.0,
            response_format={
                "type": "json_schema",
                # not strict: strict mode demands every property be required and
                # additionalProperties false, and half this schema is optional
                "json_schema": {"name": "playbook", "schema": RESPONSE_SCHEMA},
            },
        )
        return completion.choices[0].message.content or ""

    # ── assembly ─────────────────────────────────────────────────────────

    @staticmethod
    def _assemble(
        trajectory: Trajectory,
        compiled: dict,
        version: str,
        healed_from: str | None,
        heal_reason: str | None,
    ) -> ConnectorVersion:
        """Selectors come from the recording, never from the model — the model
        decides *shape*, the browser decided *truth*."""
        by_index = {s.index: s for s in trajectory.steps}
        steps: list[PlaybookStep] = []

        for item in compiled.get("steps", []):
            if not isinstance(item, dict):
                continue
            recorded = by_index.get(item.get("index"))
            if not recorded:
                continue
            try:
                action = Action(item["action"])
            except (KeyError, ValueError):
                continue  # a step the model mislabelled is dropped, not fatal

            # A recorded {{token}} is a credential the browser substituted. Force
            # it to a value_from binding regardless of what the model proposed —
            # a literal here would bake a placeholder into the playbook.
            recorded_value = recorded.value or ""
            if recorded_value.startswith("{{") and recorded_value.endswith("}}"):
                item = {**item, "value_from": recorded_value[2:-2].strip(), "value": None}

            steps.append(
                PlaybookStep(
                    index=item["index"],
                    action=Action(item["action"]),
                    selector=recorded.selector,
                    url=recorded.url if action == Action.NAVIGATE else None,
                    value_from=item.get("value_from") or None,
                    value=item.get("value") or (recorded.value if not item.get("value_from") else None),
                    expect_text=_verified_text(item.get("expect_text"), [recorded.page_text]),
                    # from the recording, not the model: whether the keystroke
                    # sent the form is a fact about the run, not a judgement
                    submits=recorded.submits,
                )
            )

        return ConnectorVersion(
            version=version,
            healed_from=healed_from,
            heal_reason=heal_reason,
            steps=steps,
            inputs=Distiller._with_examples(
                _sanitise_inputs(compiled.get("inputs")),
                compiled.get("steps", []),
                by_index,
            ),
            source_run_id=trajectory.run_id,
        )

    @staticmethod
    def _with_examples(
        fields: list[dict], steps: list[dict], by_index: dict[int, TrajectoryStep]
    ) -> list[InputField]:
        """Backfill a missing example with the value the recording actually used.

        The canary replays `example` when it is present, so an input without one
        gets synthetic gibberish — and a search box fed gibberish lands on a
        page unlike anything the playbook was compiled against. Credential
        tokens are never examples; the browser substituted those values.
        """
        inputs = [InputField(**field) for field in fields]
        by_name = {field.name: field for field in inputs}
        for item in steps:
            bound = item.get("value_from")
            if not bound or bound not in by_name:
                continue
            field = by_name[bound]
            if field.example:
                continue
            recorded = by_index.get(item.get("index"))
            value = recorded.value if recorded else None
            if value and not (value.startswith("{{") and value.endswith("}}")):
                field.example = str(value)[:120]
        return inputs

    def recompile_step(
        self,
        previous: ConnectorVersion,
        trajectory: Trajectory,
        failed_index: int,
        version: str,
        reason: str,
    ) -> ConnectorVersion:
        """Healing path: keep the version that worked, swap the step that broke.

        Only the failed step is replaced, so a heal cannot quietly rewrite the
        parts of the playbook nobody complained about. Assertions are checked
        against what the recovery run actually saw — a surviving assertion the
        page never said would only fail the next canary.
        """
        replacement = next((s for s in trajectory.steps if s.selector), None)
        corpus = [s.page_text for s in trajectory.steps]
        steps = [s.model_copy(deep=True) for s in previous.steps]
        for step in steps:
            if step.index == failed_index and replacement:
                step.selector = replacement.selector
                if replacement.value and not step.value_from:
                    step.value = replacement.value
            # re-anchor assertions on what the page really says now
            if step.expect_text:
                step.expect_text = _verified_text(step.expect_text, corpus)
        return ConnectorVersion(
            version=version,
            healed_from=previous.version,
            heal_reason=reason,
            steps=steps,
            inputs=[f.model_copy() for f in previous.inputs],
            source_run_id=trajectory.run_id,
        )
