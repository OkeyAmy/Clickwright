"""The distiller decides what a playbook asserts. These tests keep its
assertions honest: text the page never said must not reach a connector,
because the runtime compares assertions literally and a fictional one fails
every replay forever — which is exactly what the foxnews-com heal loop was.
"""

from unittest.mock import MagicMock

from app.agents.distiller import Distiller, _verified_text
from app.connectors.models import Action, PlaybookStep, Selector, Trajectory, TrajectoryStep


def test_an_assertion_the_page_never_said_is_dropped():
    # blindfold: contract — prose that appears nowhere in the snapshot is fiction
    assert _verified_text("Search results are displayed", ["Welcome to Example"]) is None
    # blindfold: contract — whitespace differences are not evidence of drift
    assert _verified_text("Welcome  to Example", ["line one\nWelcome to Example\nline two"]) == (
        "Welcome  to Example"
    )


def test_an_uncheckable_assertion_stands_rather_than_getting_nuked():
    """An empty corpus means the recorder could not read the page. Dropping every
    assertion on that basis would strip good ones whenever a snapshot failed."""
    assert _verified_text("Claim received", [None]) == "Claim received"
    assert _verified_text("Claim received", []) == "Claim received"


def _trajectory(*steps: TrajectoryStep) -> Trajectory:
    return Trajectory(run_id="run_t", connector_id="c", goal="g", model="m", steps=list(steps))


def test_compile_drops_a_fictional_assertion_and_keeps_a_real_one():
    recorded = TrajectoryStep(
        index=1,
        action=Action.CLICK,
        selector=Selector(primary="#go"),
        page_text="Acme Dashboard — Report filed successfully",
    )
    compiled = {
        "inputs": [],
        "steps": [
            {"index": 1, "action": "click", "expect_text": "Search results are displayed"},
        ],
    }

    version = Distiller._assemble(_trajectory(recorded), compiled, "1.0.0", None, None)

    # blindfold: contract — the model's invented outcome never reaches the playbook
    assert version.steps[0].expect_text is None

    compiled["steps"][0]["expect_text"] = "Report filed successfully"
    version = Distiller._assemble(_trajectory(recorded), compiled, "1.0.0", None, None)
    assert version.steps[0].expect_text == "Report filed successfully"


def test_recompile_sanitises_assertions_the_recovery_never_saw():
    prior_version = _version(
        PlaybookStep(index=1, action=Action.CLICK, selector=Selector(primary="#a"),
                     expect_text="Claim received"),
        PlaybookStep(index=2, action=Action.CLICK, selector=Selector(primary="#b"),
                     expect_text="Fictional confirmation sentence"),
    )
    recovery = _trajectory(
        TrajectoryStep(index=1, action=Action.NAVIGATE, page_text="form submitted — Claim received"),
    )

    distiller = Distiller(client=MagicMock())
    healed = distiller.recompile_step(
        previous=prior_version, trajectory=recovery, failed_index=2,
        version="1.1.0", reason="no selector matched",
    )

    # blindfold: contract — an assertion the live page still says survives
    assert healed.steps[0].expect_text == "Claim received"
    # blindfold: contract — an assertion no screen of the recovery run said is gone
    assert healed.steps[1].expect_text is None
    assert healed.healed_from == prior_version.version


def test_a_missing_example_is_backfilled_from_the_recording():
    """The canary replays `example`. Without one it types synthetic gibberish,
    and a search box fed gibberish lands on a page unlike anything explored."""
    recorded = TrajectoryStep(index=1, action=Action.TYPE, selector=Selector(primary="#q"),
                              value="Grace Hopper")
    compiled = {
        "inputs": [{"name": "search_query", "type": "string", "required": True,
                    "description": "what to search for", "example": None}],
        "steps": [{"index": 1, "action": "type", "value_from": "search_query"}],
    }

    version = Distiller._assemble(_trajectory(recorded), compiled, "1.0.0", None, None)

    # blindfold: contract — the recorded literal becomes the input's example
    assert version.inputs[0].example == "Grace Hopper"


def test_a_credential_token_never_becomes_an_example():
    """`{{password}}` in a recording is a placeholder the browser substituted;
    as an example it would hand the canary — and the OpenAPI doc — the token."""
    recorded = TrajectoryStep(index=1, action=Action.TYPE, selector=Selector(primary="#pw"),
                              value="{{password}}")
    compiled = {
        "inputs": [{"name": "password", "type": "string", "required": True,
                    "description": "sign-in secret", "example": None}],
        "steps": [{"index": 1, "action": "type", "value_from": "password"}],
    }

    version = Distiller._assemble(_trajectory(recorded), compiled, "1.0.0", None, None)

    # blindfold: invariant — no credential token leaks into inputs
    assert version.inputs[0].example is None


def test_an_existing_example_is_left_alone():
    recorded = TrajectoryStep(index=1, action=Action.TYPE, selector=Selector(primary="#t"),
                              value="INV-2")
    compiled = {
        "inputs": [{"name": "invoice_ref", "type": "string", "required": True,
                    "description": "supplier invoice reference", "example": "INV-1"}],
        "steps": [{"index": 1, "action": "type", "value_from": "invoice_ref"}],
    }

    version = Distiller._assemble(_trajectory(recorded), compiled, "1.0.0", None, None)

    # blindfold: contract — the model's example wins; backfill only fills gaps
    assert version.inputs[0].example == "INV-1"


def _version(*steps: PlaybookStep):
    from app.connectors.models import ConnectorVersion

    return ConnectorVersion(version="1.0.0", steps=list(steps))


def test_a_schema_violating_reply_no_longer_kills_the_compile():
    """Observed live on a real CURA exploration: 17 browser steps succeeded,
    then one wrong enum in the model's reply raised ValidationError and threw
    the whole run away. The assembler must absorb it instead."""
    recorded = TrajectoryStep(index=1, action=Action.TYPE, selector=Selector(primary="#f"),
                              value="Seoul")
    compiled = {
        "inputs": [
            {"name": "Facility!", "type": "text", "required": "yes"},   # bad name + bad type
            {"name": "facility", "type": "string", "example": 42},       # duplicate name, non-str example
        ],
        "steps": [
            {"index": 1, "action": "typo"},                             # unknown action — drop
            {"index": 1, "action": "type", "value_from": "facility"},
        ],
    }

    version = Distiller._assemble(_trajectory(recorded), compiled, "1.0.0", None, None)

    # blindfold: contract — inputs are usable regardless of what arrived
    assert [i.name for i in version.inputs] == ["facility", "facility_2"]
    assert all(i.type in ("string", "number", "boolean") for i in version.inputs)
    assert version.inputs[1].example == "42"
    # blindfold: contract — the valid step survives, the mislabelled one is dropped
    assert [s.index for s in version.steps] == [1]
