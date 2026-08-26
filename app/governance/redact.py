"""PII redaction before anything is persisted or shown.

Runs on every trajectory before it reaches the registry, the run store or the
dashboard, so an audit trail never becomes the leak it was meant to prevent.
"""

from __future__ import annotations

import re

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("[email]", re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")),
    ("[card]", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("[ssn]", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # \b before an optional "+" never matches, so the leading + survived redaction
    ("[phone]", re.compile(r"(?<![\w+])\+?\d[\d ()-]{7,}\d\b")),
    ("[iban]", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")),
]


def redact(text: str | None) -> str | None:
    if not text:
        return text
    for token, pattern in PATTERNS:
        text = pattern.sub(token, text)
    return text


def redact_step(step) -> None:
    """In place, on a TrajectoryStep."""
    step.reason = redact(step.reason)
    step.page_text = redact(step.page_text)
    if step.value and not (step.selector and _is_credential(step.selector.primary)):
        step.value = redact(step.value)


def _is_credential(selector: str) -> bool:
    return any(token in selector.lower() for token in ("pass", "pwd", "secret", "token"))
