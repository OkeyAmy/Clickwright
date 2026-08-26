"""Repair-strategy baselines, following the web-test-repair literature.

The field evaluates self-healing by injecting a locator break and comparing
repair strategies on accuracy and recovery time (Hammoudi et al. — why
record/replay breaks; Leotta/Stocco et al. — Robula+/Similo multi-attribute
matching; Nass et al. — VON Similo LLM; Xu et al. — explanation-checked LLM
repair). This module implements the standard contenders so the benchmark
compares Clickwright against them, not against itself:

  · static floor          no repair — the locator is dead until a human acts
  · attribute similarity  score live elements against the recorded metadata
                          (tag / text / accessible name), swap in the winner —
                          the deterministic strategy behind Healenium-class tools
  · LLM single-step       send the recorded metadata plus top-K scored
                          candidates to the model and let it choose — the
                          VON Similo-LLM protocol
  · clickwright           escalate that one step back to computer use with
                          full task context, verify, republish (the product)

All strategies replay through the same driver and see the same injected fault.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Awaitable

from playwright.async_api import Page

from app.agents.healer import Healer
from app.connectors.models import ConnectorVersion, Selector
from app.connectors.registry import Registry
from app.connectors.runtime import ConnectorRuntime, StepFailure

RepairFn = Callable[[Page, Any], Awaitable[Selector | None]]

CANDIDATES_JS = """
() => [...document.querySelectorAll(
  'button, a[href], input, select, textarea, [role="button"]'
)].map(el => ({
  tag: el.tagName.toLowerCase(),
  id: el.id || null,
  name: el.getAttribute('name'),
  aria: (el.getAttribute('aria-label') || '').trim() || null,
  placeholder: el.getAttribute('placeholder') || null,
  type: el.getAttribute('type'),
  value: (el.getAttribute('value') || '').trim() || null,
  text: (el.textContent || '').trim().slice(0, 60) || null,
})).slice(0, 400)
"""


def _tokens(text: str | None) -> set[str]:
    return {t for t in ((text or "").lower().split()) if t}


def similarity_score(cand: dict, selector: Selector) -> float:
    """Attribute-similarity in the Similo spirit: tag agreement plus labelled-
    text overlap. Weights are simple and stated; this is a baseline, not a
    contribution."""
    score = 0.0
    if cand.get("aria"):
        if (selector.accessible_name or "").strip().lower() == cand["aria"].strip().lower():
            score += 1.0
        else:
            overlap = _tokens(selector.accessible_name) & _tokens(cand["aria"])
            if overlap:
                score += 0.6 * len(overlap) / max(len(_tokens(selector.accessible_name)), 1)
    for field, weight in (("text", 1.2), ("value", 1.0)):
        if cand.get(field):
            if (selector.text or "").strip().lower() == str(cand[field]).strip().lower():
                score += weight
            else:
                overlap = _tokens(selector.text) & _tokens(str(cand[field]))
                if overlap:
                    score += weight * 0.6 * len(overlap) / max(len(_tokens(selector.text)), 1)
    return score


def selector_for(candidate: dict) -> Selector:
    """The strongest locator the matched element itself supports."""
    if candidate.get("id"):
        primary = f"#{candidate['id']}"
    elif candidate.get("aria"):
        primary = f"{candidate['tag']}[aria-label=\"{candidate['aria']}\"]"
    elif candidate.get("text") and candidate["tag"] in ("button", "a"):
        primary = f"{candidate['tag']}:has-text(\"{candidate['text'][:40]}\")"
    elif candidate.get("name"):
        primary = f"{candidate['tag']}[name=\"{candidate['name']}\"]"
    else:
        primary = f"{candidate['tag']}"
    return Selector(primary=primary)


async def similarity_repair_fn(page: Page, step) -> Selector | None:
    """Score every live element against what was recorded; win takes the step."""
    try:
        elements = await page.evaluate(CANDIDATES_JS)
    except Exception:  # noqa: BLE001
        return None
    ranked = sorted(
        ((similarity_score(c, step.selector), c) for c in elements),
        key=lambda pair: pair[0],
        reverse=True,
    )
    if not ranked or ranked[0][0] <= 0.6:
        return None
    return selector_for(ranked[0][1])


async def llm_repair_fn_factory(top_k: int = 10) -> RepairFn:
    """VON-Similo-LLM protocol: rank candidates by similarity, hand the top K
    to the model with the recorded metadata, take its pick."""
    from bench.judge import _verdict_from_text

    async def repair(page: Page, step) -> Selector | None:
        try:
            elements = await page.evaluate(CANDIDATES_JS)
            ranked = sorted(
                ((similarity_score(c, step.selector), c) for c in elements),
                key=lambda p: p[0],
                reverse=True,
            )
            shortlist = [c for score, c in ranked[:top_k] if score > 0]
            if not shortlist:
                return None

            prompt = {
                "broken_step": {
                    "action": step.action.value,
                    "recorded_text": step.selector.text,
                    "recorded_accessible_name": step.selector.accessible_name,
                },
                "candidates": shortlist,
            }
            system = (
                "A web automation broke because the page changed. Given the "
                "recorded element metadata and candidate elements from the live "
                "page, reply with one JSON object: {\"index\": <candidate index>, "
                "\"reason\": \"one sentence\"}. Choose the candidate that is the "
                "same control the recording interacted with."
            )
            if not os.getenv("CLICKWRIGHT_MODEL_BASE_URL") and os.getenv("GOOGLE_API_KEY"):
                from google import genai
                from google.genai import types as gtypes

                client = genai.Client()
                response = client.models.generate_content(
                    model=os.getenv("CLICKWRIGHT_JUDGE_MODEL", "gemini-3.5-flash"),
                    contents=[{"role": "user", "parts": [
                        {"text": system}, {"text": json.dumps(prompt)},
                    ]}],
                    config=gtypes.GenerateContentConfig(temperature=0.0),
                )
                verdict = _verdict_from_text(response.text or "")
            else:
                from openai import OpenAI

                from app.agents.openai_explorer import MODEL, api_key

                client = OpenAI(api_key=api_key(), timeout=60, max_retries=2,
                                base_url=os.getenv("CLICKWRIGHT_MODEL_BASE_URL") or None)
                completion = client.chat.completions.create(
                    model=os.getenv("CLICKWRIGHT_JUDGE_MODEL") or MODEL,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": json.dumps(prompt)}],
                    temperature=0.0,
                )
                verdict = _verdict_from_text(completion.choices[0].message.content or "")

            idx = int(verdict.get("index", -1)) if str(verdict.get("index", "-1")).lstrip("-").isdigit() else -1
            if 0 <= idx < len(shortlist):
                return selector_for(shortlist[idx])
            return None
        except Exception:  # noqa: BLE001 - an unavailable judge is a failed repair
            return None

    return repair


async def replay_with_repair(
    runtime: ConnectorRuntime,
    registry: Registry,
    cid: str,
    repair_fn: RepairFn | None,
    fault_selector_step: int | None = None,
) -> dict[str, Any]:
    """Replay the active playbook whose locator was already replaced by a dead
    primary (the caller injects it), repairing through `repair_fn` when a step
    fails. One repair attempt per failing step — how the literature measures
    single-shot repair."""
    from playwright.async_api import async_playwright

    stored = registry.get(cid)
    version = stored.active()
    # same merge production does: inputs plus injected credentials — without
    # them every authenticated target fails at its login step
    from app.governance.secrets import resolve_credentials

    values = {**Healer.canary_inputs(version), **resolve_credentials(cid)}

    started = time.monotonic()
    outcome: dict[str, Any] = {"completed": False, "failed_at": None, "repaired_steps": []}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=runtime.headless)
        page = await (await browser.new_context(viewport={"width": 1280, "height": 936})).new_page()
        try:
            for step in version.steps:
                try:
                    await runtime._step(page, step, values, stored)
                except StepFailure as exc:
                    outcome["failed_at"] = exc.index
                    if repair_fn is None:
                        break
                    replacement = await repair_fn(page, step)
                    if replacement is None:
                        break
                    step.selector = replacement
                    outcome["repaired_steps"].append(step.index)
                    await runtime._step(page, step, values, stored)
            else:
                outcome["completed"] = True
        finally:
            await browser.close()
    outcome["time_s"] = round(time.monotonic() - started, 2)
    return outcome


async def repair_comparison_legs(
    runtime: ConnectorRuntime,
    scratch: Registry,
    cid: str,
    include_llm: bool = False,
    fault_step: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Run every literature baseline through the same injected fault."""
    stored = scratch.get(cid)
    good = stored.active()
    selectable = [s for s in good.steps if s.selector and s.action.value != "navigate"]
    anchor = (
        next((s for s in selectable if s.index == fault_step), None)
        or max(selectable, key=lambda s: abs(s.index - len(good.steps) / 2), default=None)
    )
    if anchor is None:
        return {}

    def inject(version: ConnectorVersion) -> ConnectorVersion:
        broken = version.model_copy(deep=True)
        for step in broken.steps:
            if step.index == anchor.index:
                # kill the locator, keep the recorded metadata — drift breaks
                # selectors, it does not erase what was recorded about them
                step.selector.primary = "#cw-bench-missing-target"
                step.selector.fallbacks = []
        return broken

    results: dict[str, dict[str, Any]] = {}
    for name, fn in (
        ("static_floor", None),
        ("attribute_similarity", similarity_repair_fn),
    ):
        scratch.publish(stored, inject(good).model_copy(update={"version": "7.0.0"}))
        results[name] = await replay_with_repair(runtime, scratch, cid, fn, fault_selector_step=anchor.index)

    if include_llm:
        llm = await llm_repair_fn_factory()
        scratch.publish(stored, inject(good).model_copy(update={"version": "7.1.0"}))
        results["llm_single_step"] = await replay_with_repair(runtime, scratch, cid, llm, fault_selector_step=anchor.index)

    # restore the healthy artifact for whatever runs next
    scratch.publish(stored, good.model_copy(deep=True, update={"version": "7.2.0"}))
    results["_anchor"] = {"injected_step": anchor.index}
    return results
