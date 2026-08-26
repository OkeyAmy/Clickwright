"""The pause: an agent stopped mid-run, waiting for a person.

Two moments matter — an action it cannot undo, and a value only a human has
(a code texted to a phone, a choice between options, a detail nobody supplied).
Both must block the run without wedging the event loop, and both must fail
closed when nobody answers.
"""

import asyncio

import pytest

from app.governance.gate import ApprovalGate
from app.governance.policy import PolicyGateway


@pytest.mark.asyncio
async def test_an_ordinary_action_is_not_held():
    """A queue that fills with "it clicked a text field" is a queue nobody reads."""
    gate = ApprovalGate()

    allowed, reason = await gate.check(
        run_id="run_1", connector_id="c", intent="Focus the search box", target="Search"
    )

    # blindfold: contract — no hold, no reason, no request recorded
    assert (allowed, reason) == (True, "")
    assert gate.pending() == []


@pytest.mark.asyncio
async def test_an_irreversible_action_waits_for_a_person():
    gate = ApprovalGate()
    task = asyncio.create_task(
        gate.check(run_id="run_1", connector_id="c", intent="Submit the order", target="Place order")
    )
    await asyncio.sleep(0)  # let it register and block

    pending = gate.pending()
    # blindfold: contract — one pending request, carrying what it is about to do
    assert len(pending) == 1
    assert pending[0].kind == "in_run"  # blindfold: contract — paused agent, not a pre-flight hold
    assert pending[0].action == "Submit the order"  # blindfold: contract — the model's own words
    assert not task.done()

    assert gate.decide(pending[0].id, True) is True
    allowed, _ = await task
    assert allowed is True
    # blindfold: contract — the trail records the decision, not just the request
    assert gate.requests[pending[0].id].status == "approved"


@pytest.mark.asyncio
async def test_a_refusal_comes_back_as_a_refusal():
    gate = ApprovalGate()
    task = asyncio.create_task(
        gate.check(run_id="run_1", connector_id="c", intent="Delete the record", target="Delete")
    )
    await asyncio.sleep(0)

    gate.decide(gate.pending()[0].id, False)
    allowed, reason = await task

    assert allowed is False
    # blindfold: contract — the reason names the rule that stopped it
    assert "destroys" in reason


@pytest.mark.asyncio
async def test_nobody_answering_is_a_refusal_not_a_hung_run():
    """The browser is open and a step is half-finished the whole time this
    waits. Failing closed releases both."""
    gate = ApprovalGate(timeout_s=0.05)

    allowed, reason = await gate.check(
        run_id="run_1", connector_id="c", intent="Confirm payment", target="Pay now"
    )

    assert allowed is False
    # blindfold: contract — the trail says nobody answered, not that it was denied
    assert "no decision within" in reason


@pytest.mark.asyncio
async def test_the_agent_can_ask_for_a_value_only_a_human_has():
    """A one-time code, a choice, a missing detail. No model produces these."""
    gate = ApprovalGate()
    task = asyncio.create_task(
        gate.ask(run_id="run_1", connector_id="c", question="Enter the code sent to your phone")
    )
    await asyncio.sleep(0)

    pending = gate.pending()
    # blindfold: contract — a question to answer, not an action to approve
    assert pending[0].kind == "input_needed"
    assert gate.provide(pending[0].id, "482913") is True

    # blindfold: contract — the value reaches the run that asked for it
    assert await task == "482913"


@pytest.mark.asyncio
async def test_an_unanswered_question_does_not_strand_the_run():
    gate = ApprovalGate(timeout_s=0.05)

    answer = await gate.ask(run_id="run_1", connector_id="c", question="Which address?")

    # blindfold: contract — None, so the caller reports what is left undone
    assert answer is None
    request = next(iter(gate.requests.values()))
    # blindfold: contract — recorded as denied, with why, and no longer pending
    assert request.status == "denied"
    assert request.reason == "Which address? (no answer within 0.05s)"
    assert gate.pending() == []


def test_deciding_something_nobody_waits_on_is_reported_not_silent():
    """A stale console tab clicking approve must not look like it worked."""
    gate = ApprovalGate()

    # blindfold: contract — False, so the endpoint can answer 409 rather than 200
    assert gate.decide("apr_gone", True) is False
    assert gate.provide("apr_gone", "123") is False


@pytest.mark.parametrize(
    "intent,expected",
    [
        ("Click the Sign in button", False),
        ("Scroll down to see the rest", False),
        ("Submit the claim", True),
        ("Delete this record", True),
        ("Confirm and pay", True),
        ("Book the appointment", True),
    ],
)
def test_which_intents_need_a_person(intent, expected):
    """Not a finance rule: anything the agent cannot take back."""
    assert bool(PolicyGateway().consequential(intent, "")) is expected
