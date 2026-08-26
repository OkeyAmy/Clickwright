"""Policy gateway — one boundary in front of every connector.

Two jobs:
  1. Hold actions the model's own safety policies classify as consequential
     (financial transactions, destructive changes) for human approval.
  2. Record what was decided, so the audit trail answers "why did it do that".

The model-side controls (prompt-injection detection, built-in safety policies)
stay enabled on the computer-use tool; this gateway is what happens *after* a
connector is compiled, where there is no model left to ask.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.connectors.models import Connector, PolicyEvent

# Text that tries to redirect an agent mid-task. The model flags these too;
# this is the deterministic net underneath it.
INJECTION_PATTERNS = [
    # qualifiers stack in the wild ("ignore all previous instructions"), so repeat the group
    re.compile(r"ignore (?:all |your |previous |prior |the )*instructions", re.I),
    re.compile(r"disregard (?:the |your |all )*(?:above|previous|system)", re.I),
    re.compile(r"(send|email|upload|post) (the |all )?(csv|data|records|credentials)", re.I),
    re.compile(r"you are now (a|an|in) ", re.I),
    re.compile(r"</?(system|assistant)>", re.I),
]

FINANCIAL_FIELDS = ("amount", "amt", "total", "payment", "iban", "account")

# Actions an exploring agent cannot take back. Matched against the model's own
# stated intent and the control it resolved to, because during exploration
# nobody has seen the steps yet — the irreversible one arrives unannounced.
IRREVERSIBLE = [
    (re.compile(r"\b(pay|payment|purchase|checkout|place (the )?order|transfer funds?)\b"),
     "money moves"),
    (re.compile(r"\b(delete|remove|revoke|cancel|terminate|deactivate)\b"),
     "it destroys something"),
    (re.compile(r"\b(submit|send|confirm|file|book|approve|publish)\b"),
     "it commits the form to the system"),
]


@dataclass
class Decision:
    hold: bool = False
    reason: str = ""
    events: list[PolicyEvent] = field(default_factory=list)


class PolicyGateway:
    def __init__(self, financial_threshold: float = 1000.0):
        self.financial_threshold = financial_threshold

    def evaluate(self, connector: Connector, inputs: dict) -> Decision:
        decision = Decision()

        if connector.requires_approval:
            decision.hold = True
            decision.reason = f"{connector.id} is marked as requiring approval"

        for key, value in inputs.items():
            if any(token in key.lower() for token in FINANCIAL_FIELDS):
                amount = self._as_amount(value)
                if amount is not None and amount >= self.financial_threshold:
                    decision.hold = True
                    decision.reason = (
                        f"{key}={amount} meets the financial-transaction threshold "
                        f"({self.financial_threshold})"
                    )
                    decision.events.append(
                        PolicyEvent(
                            kind="safety",
                            detail=decision.reason,
                            action_taken="held",
                        )
                    )

        for key, value in inputs.items():
            hit = self.scan_text(str(value))
            if hit:
                decision.events.append(
                    PolicyEvent(
                        kind="injection",
                        detail=f"input {key!r} matched {hit!r}",
                        action_taken="blocked",
                    )
                )
                decision.hold = True
                decision.reason = f"input {key!r} contains an instruction-style payload"

        return decision

    def consequential(self, intent: str, target: str) -> str:
        """Why this mid-run action needs a human, or "" if it does not.

        A compiled connector is checked once, before it runs, because its steps
        are known. An exploring agent is the opposite: nobody has seen the steps
        yet, and the irreversible one arrives without warning. This is the check
        that stands between "the model decided to submit" and "it submitted".
        """
        haystack = f"{intent} {target}".lower()
        for pattern, why in IRREVERSIBLE:
            match = pattern.search(haystack)
            if match:
                return f"about to {match.group(0)} — {why}"
        return ""

    @staticmethod
    def scan_text(text: str) -> str | None:
        """Returns the matched fragment if the text tries to steer an agent."""
        for pattern in INJECTION_PATTERNS:
            match = pattern.search(text or "")
            if match:
                return match.group(0)
        return None

    @staticmethod
    def _as_amount(value) -> float | None:
        try:
            return float(str(value).replace(",", "").replace("$", "").strip())
        except (TypeError, ValueError):
            return None
