"""The console's half of the pause: see it, answer it, watch the run resume.

The gate itself is covered in test_gate.py. What matters here is the wiring —
a paused run must appear in the pending queue an operator polls, and a decision
made through the API must reach the coroutine that is actually blocked.
"""

import asyncio

import pytest

from app.governance.gate import ApprovalGate


@pytest.fixture
def server(tmp_path, monkeypatch):
    """The API module with its stores pointed at a scratch directory.

    Reloading the module is not enough: `app.store` reads CLICKWRIGHT_HOME once,
    at first import, so a reloaded server rebinds to the *real* var/ and the
    tests write approvals into it.
    """
    import app.server as module
    from app.store import Store

    monkeypatch.setattr(module, "store", Store(home=tmp_path))
    module.events = module.EventBus()
    monkeypatch.setattr(  # only annotated at module scope; lifespan assigns it
        module,
        "gate",
        ApprovalGate(
            publish=lambda kind, payload: module.events.publish(kind, payload),
            record=module.store.save_approval,
        ),
        raising=False,
    )
    return module


@pytest.mark.asyncio
async def test_a_paused_run_appears_in_the_queue_and_resumes_on_approval(server):
    held = asyncio.create_task(
        server.gate.check(
            run_id="run_1", connector_id="c", intent="Submit the claim", target="Submit"
        )
    )
    await asyncio.sleep(0)

    pending = server.list_approvals()
    # blindfold: contract — the operator sees the paused run, not just a log line
    assert len(pending) == 1
    assert pending[0]["kind"] == "in_run"  # blindfold: contract — a paused agent
    assert pending[0]["action"] == "Submit the claim"  # blindfold: contract — the model's words
    assert not held.done()

    decided = await server.decide_approval(pending[0]["id"], "approve")

    # blindfold: contract — the blocked coroutine is released, not left waiting
    allowed, _ = await asyncio.wait_for(held, timeout=1)
    assert allowed is True
    # blindfold: contract — the endpoint reports the decision it applied
    assert decided["status"] == "approved"


@pytest.mark.asyncio
async def test_denying_reaches_the_run_that_is_waiting(server):
    held = asyncio.create_task(
        server.gate.check(run_id="run_1", connector_id="c", intent="Delete it", target="Delete")
    )
    await asyncio.sleep(0)

    await server.decide_approval(server.list_approvals()[0]["id"], "deny")

    allowed, reason = await asyncio.wait_for(held, timeout=1)
    assert allowed is False
    # blindfold: contract — the refusal explains itself to the model and the trail
    assert "destroys" in reason


@pytest.mark.asyncio
async def test_an_answer_reaches_the_agent_but_is_never_stored(server):
    """A one-time code in the audit trail is a one-time code in a backup."""
    asked = asyncio.create_task(
        server.gate.ask(run_id="run_1", connector_id="c", question="Code sent to your phone?")
    )
    await asyncio.sleep(0)
    approval_id = server.list_approvals()[0]["id"]

    answered = server.answer_approval(approval_id, server.AnswerRequest(value="482913"))

    assert answered == {"id": approval_id, "answered": True}
    # blindfold: contract — the value reaches the run that asked
    assert await asyncio.wait_for(asked, timeout=1) == "482913"
    # blindfold: contract — and nowhere else: the record keeps the question only
    stored = server.store.approvals.get(approval_id)
    assert "482913" not in stored.model_dump_json()
    # blindfold: contract — the question survives, verbatim, as the audit record
    assert stored.reason == "Code sent to your phone?"


@pytest.mark.asyncio
async def test_answering_something_nobody_waits_on_is_rejected(server):
    """A stale console tab must not silently succeed."""
    from fastapi import HTTPException

    asked = asyncio.create_task(
        server.gate.ask(run_id="run_1", connector_id="c", question="Which one?")
    )
    await asyncio.sleep(0)
    approval_id = server.list_approvals()[0]["id"]
    server.gate.provide(approval_id, "first")
    await asyncio.wait_for(asked, timeout=1)

    with pytest.raises(HTTPException) as caught:
        server.answer_approval(approval_id, server.AnswerRequest(value="second"))

    # blindfold: contract — 404, because the request is no longer pending
    assert caught.value.status_code == 404


@pytest.mark.asyncio
async def test_a_second_canary_while_one_runs_is_declined(server, registry_home):
    """Two clicks, two twin explorations racing to the registry — that is how
    the duplicate 1.1.0 happened. The endpoint refuses the overlap instead."""
    from fastapi import BackgroundTasks, HTTPException

    from app.connectors.registry import Registry
    from tests.factories import vendor_connector, vendor_version

    monkey_registry = Registry(registry_home / "registry")
    monkey_registry.publish(vendor_connector("http://x"), vendor_version("http://x"))
    import app.server as module

    module.registry = monkey_registry
    module._heal_inflight.clear()

    accepted = await module.heal("vendor-portal", BackgroundTasks())
    # blindfold: contract — the first request is queued normally
    assert accepted["accepted"] is True

    try:
        with pytest.raises(HTTPException) as caught:
            await module.heal("vendor-portal", BackgroundTasks())
    finally:
        module._heal_inflight.clear()

    assert caught.value.status_code == 409
