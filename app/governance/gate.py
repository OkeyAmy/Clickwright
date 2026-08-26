"""The pause — the two moments an agent driving a browser needs a person.

  1. It is about to do something it cannot undo. Submit, send, delete, book,
     pay. A compiled connector can be checked before it runs because its steps
     are known; an exploring agent cannot, because nobody has seen the steps
     yet and the irreversible one arrives unannounced.

  2. It needs something only a person has. A one-time code texted to a phone,
     a code from an authenticator app, an answer to a security question. No
     amount of model capability produces that value.

Both are the same shape: stop, show the operator the screen the agent is
looking at, wait for a human, carry on. The design constraint is that waiting
must not wedge anything — the browser stays open, the event loop stays free,
and a request nobody answers times out rather than hanging the run.

A supplied code is handled like any other credential: it goes into the
browser's secret table, and the agent is told to type `{{otp}}`, so the value
itself never enters model context or the recorded trajectory.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Callable, Optional

from app.connectors.models import ApprovalRequest
from app.governance.policy import PolicyGateway

# Long enough for a person to look at a screenshot and decide, short enough that
# a forgotten run releases the browser instead of holding it overnight.
DECISION_TIMEOUT_S = 600


class ApprovalGate:
    """Asks a human before a consequential action, and blocks until they answer.

    `publish` puts the request in front of the operator (the console's event
    stream); `record` persists it so the audit trail keeps both the hold and
    what followed. Either may be omitted in tests.
    """

    def __init__(
        self,
        publish: Optional[Callable[[str, dict], None]] = None,
        record: Optional[Callable[[ApprovalRequest], None]] = None,
        gateway: Optional[PolicyGateway] = None,
        timeout_s: int = DECISION_TIMEOUT_S,
    ):
        self._publish = publish
        self._record = record
        self.gateway = gateway or PolicyGateway()
        self.timeout_s = timeout_s
        self._waiting: dict[str, asyncio.Future[bool]] = {}
        self.requests: dict[str, ApprovalRequest] = {}

    def pending(self) -> list[ApprovalRequest]:
        return [r for r in self.requests.values() if r.status == "pending"]

    async def check(
        self,
        *,
        run_id: str,
        connector_id: str,
        intent: str,
        target: str,
        screenshot: Optional[str] = None,
    ) -> tuple[bool, str]:
        """(allowed, reason). Returns immediately unless the action is consequential."""
        reason = self.gateway.consequential(intent, target)
        if not reason:
            return True, ""

        request = ApprovalRequest(
            id=f"apr_{uuid.uuid4().hex[:6]}",
            run_id=run_id,
            connector_id=connector_id,
            reason=reason,
            kind="in_run",
            action=intent or "(no stated intent)",
            target=target or None,
            screenshot=screenshot,
        )
        self.requests[request.id] = request
        if self._record:
            self._record(request)
        if self._publish:
            self._publish("approval.requested", {"approval": request.model_dump()})

        loop = asyncio.get_running_loop()
        decision: asyncio.Future[bool] = loop.create_future()
        self._waiting[request.id] = decision
        try:
            approved = await asyncio.wait_for(asyncio.shield(decision), self.timeout_s)
        except asyncio.TimeoutError:
            # Nobody answered. Refusing is the safe direction: the run reports a
            # blocked action, which is recoverable; a wrong payment is not.
            approved = False
            request.reason = f"{reason} (no decision within {self.timeout_s}s)"
        finally:
            self._waiting.pop(request.id, None)

        request.status = "approved" if approved else "denied"
        if self._record:
            self._record(request)
        if self._publish:
            self._publish("approval.decided", {"approval": request.model_dump()})
        return approved, request.reason

    async def ask(
        self,
        *,
        run_id: str,
        connector_id: str,
        question: str,
        screenshot: Optional[str] = None,
    ) -> Optional[str]:
        """Ask the operator for a value the agent cannot obtain — an OTP, a code
        from an authenticator, an answer only they know. None if nobody answers.
        """
        request = ApprovalRequest(
            id=f"apr_{uuid.uuid4().hex[:6]}",
            run_id=run_id,
            connector_id=connector_id,
            reason=question,
            kind="input_needed",
            action=question,
            screenshot=screenshot,
        )
        self.requests[request.id] = request
        if self._record:
            self._record(request)
        if self._publish:
            self._publish("approval.requested", {"approval": request.model_dump()})

        loop = asyncio.get_running_loop()
        answer: asyncio.Future[Optional[str]] = loop.create_future()
        self._waiting[request.id] = answer
        try:
            value = await asyncio.wait_for(asyncio.shield(answer), self.timeout_s)
        except asyncio.TimeoutError:
            value = None
            request.reason = f"{question} (no answer within {self.timeout_s}s)"
        finally:
            self._waiting.pop(request.id, None)

        request.status = "approved" if value else "denied"
        if self._record:
            self._record(request)
        if self._publish:
            # the value never travels: the console only needs to stop showing it
            self._publish("approval.decided", {"approval": request.model_dump()})
        return value

    def decide(self, approval_id: str, approved: bool) -> bool:
        """Release a waiting run. False if nothing was waiting on this id."""
        return self._release(approval_id, approved)

    def provide(self, approval_id: str, value: str) -> bool:
        """Hand a waiting run the value it asked for."""
        return self._release(approval_id, value)

    def _release(self, approval_id: str, result) -> bool:
        waiting = self._waiting.get(approval_id)
        if waiting is None or waiting.done():
            return False
        waiting.set_result(result)
        return True
