"""Which explorer drives the browser.

Gemini through ADK is the default: computer use is the model provider's own, so
the action vocabulary and the screenshot round-trip come for free. Setting a
base URL switches to the OpenAI-compatible backend, which speaks a different
protocol entirely — OpenRouter, a gateway, a local server — and therefore owns
the loop itself.

Both return the same Trajectory, so everything downstream is unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def build_explorer(artifacts_dir: Optional[Path] = None, headless: bool = True, gate=None):
    if os.getenv("CLICKWRIGHT_MODEL_BASE_URL"):
        from app.agents.openai_explorer import OpenAIExplorer

        return OpenAIExplorer(artifacts_dir=artifacts_dir, headless=headless, gate=gate)

    from app.agents.explorer import Explorer

    return Explorer(artifacts_dir=artifacts_dir, headless=headless, gate=gate)
