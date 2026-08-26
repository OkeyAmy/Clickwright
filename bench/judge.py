"""LLM-as-judge for the exploration leg — the WebVoyager protocol, trimmed.

The judge sees the task, the final URL, and either the last screenshot or the
page text captured after it, and answers one question: did the agent finish
what was asked? The verdict is strict JSON so the report can aggregate it.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Optional

SYSTEM = """\
You grade web agents. You are given a task and the state the agent ended on. \
Answer with one JSON object and nothing else:
{"success": true|false, "reason": "one sentence"}
Success means the task's outcome is visibly achieved on this final state — \
not merely attempted. If evidence is missing, success is false.\
"""


def _final_screen(step) -> tuple[Optional[bytes], str]:
    """(screenshot_bytes_or_None, page_text) from the trajectory's last step."""
    shot: Optional[bytes] = None
    path = getattr(step, "screenshot", None)
    if path and Path(path).is_file():
        shot = Path(path).read_bytes()
    return shot, (getattr(step, "page_text", None) or "")


def _verdict_from_text(text: str) -> dict[str, Any]:
    """Parse the judge's reply; tolerate a code fence or trailing prose."""
    candidate = text.strip()
    if "```" in candidate:
        inside = candidate.split("```")[1]
        candidate = inside[4:] if inside.lower().startswith("json") else inside
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1:
        try:
            parsed = json.loads(candidate[start : end + 1])
            if isinstance(parsed.get("success"), bool):
                return {"success": parsed["success"], "reason": str(parsed.get("reason", ""))[:200]}
        except ValueError:
            pass
    return {"success": False, "reason": f"unparseable verdict: {text[:150]}"}


def _ask_gemini(user_parts: list[dict[str, Any]]) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client()
    model = os.getenv("CLICKWRIGHT_JUDGE_MODEL", os.getenv("CLICKWRIGHT_DISTILLER_MODEL", "gemini-3.5-flash"))
    response = client.models.generate_content(
        model=model,
        contents=user_parts,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            temperature=0.0,
            response_mime_type="application/json",
        ),
    )
    return response.text or ""


def _ask_openai(user_content: list[dict[str, Any]]) -> str:
    from openai import OpenAI

    from app.agents.openai_explorer import MODEL, api_key

    client = OpenAI(
        api_key=api_key(),
        base_url=os.getenv("CLICKWRIGHT_MODEL_BASE_URL") or None,
        max_retries=int(os.getenv("CLICKWRIGHT_MODEL_RETRIES", "8")),
        timeout=120,
    )
    completion = client.chat.completions.create(
        model=os.getenv("CLICKWRIGHT_JUDGE_MODEL") or MODEL,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_content},
        ],
        temperature=0.0,
        response_format={"type": "json_object"},
    )
    return completion.choices[0].message.content or ""


def judge_task(goal: str, final_url: str, step) -> dict[str, Any]:
    """Grade one finished exploration. Never raises — a judge outage is a
    verdict of 'unknown', not a crashed benchmark run."""
    try:
        screenshot, page_text = _final_screen(step)
        text_block = (
            f"Task: {goal}\nFinal URL: {final_url}\n\n"
            f"Visible page text:\n{page_text[:3000]}"
        )
        if screenshot:
            image_b64 = base64.b64encode(screenshot).decode()
            user_parts = [{"role": "user", "parts": [
                {"text": text_block},
                {"inline_data": {"mime_type": "image/png", "data": image_b64}},
            ]}]
            user_openai = [
                {"type": "text", "text": text_block},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image_b64}},
            ]
        else:
            user_parts = [{"role": "user", "parts": [{"text": text_block}]}]
            user_openai = [{"type": "text", "text": text_block}]

        if not os.getenv("CLICKWRIGHT_MODEL_BASE_URL") and os.getenv("GOOGLE_API_KEY"):
            reply = _ask_gemini(user_parts)
        else:
            reply = _ask_openai(user_openai)
        return _verdict_from_text(reply)
    except Exception as exc:  # noqa: BLE001 - judging must not kill the suite
        return {"success": False, "unknown": True, "reason": f"judge unavailable: {exc}"}
