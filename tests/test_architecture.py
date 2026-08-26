"""The architecture benchmark must itself be trustworthy.

These run the three legs against the bundled portal with no model anywhere:
a corrupted selector is detected at exactly its own index, recovery returns
green, and the report renders honest aggregates.
"""

import json

import pytest

from app.agents.healer import Healer
from app.connectors.registry import Registry
from app.connectors.runtime import ConnectorRuntime
from app.connectors.models import Selector
from app.connectors.models import Selector
from bench.repair import (
    replay_with_repair,
    similarity_repair_fn,
    similarity_score,
)
from bench.run_suite import (
    artifact_metrics,
    detection_leg_instrumented,
    render_report,
    selector_class,
    step_durability,
)
from tests.factories import CLAIM, sel, vendor_connector, vendor_version


def test_similarity_scoring_prefers_recorded_text_and_labels():
    from app.connectors.models import Selector as S

    recorded = S(primary="#gone", text="Sign In")
    exact = {"tag": "button", "text": "Sign In"}
    partial = {"tag": "button", "text": "Sign up for alerts"}
    unrelated = {"tag": "input", "text": None}

    # blindfold: contract — the element whose label matches the recording wins
    assert similarity_score(exact, recorded) > similarity_score(partial, recorded)
    assert similarity_score(unrelated, recorded) == 0.0


@pytest.mark.asyncio
async def test_attribute_similarity_repairs_a_broken_locator(portal, tmp_path):
    """The Similo-style baseline: metadata recorded at compile time (the
    button's label) scores the live page and the winner takes over the step."""
    runtime = ConnectorRuntime()
    scratch = Registry(tmp_path / "registry")

    version = vendor_version(portal)
    # what the recorder would have captured alongside the ids
    version.steps[3].selector.text = "Sign In"      # the sign-in button
    version.steps[-1].selector.text = "Submit Claim"
    # the fault: drift renamed this control; only the recorded metadata survives
    version.steps[3].selector.primary = "#ctl00_signin-gone"
    version.steps[3].selector.fallbacks = []
    scratch.publish(vendor_connector(portal), version)

    result = await replay_with_repair(
        runtime, scratch, "vendor-portal", similarity_repair_fn,
        fault_selector_step=4,  # break the sign-in button's locator
    )

    # blindfold: contract — the dead locator is replaced and the run completes
    assert result["failed_at"] == 4
    assert result["completed"] is True, result
    assert result["repaired_steps"] == [4]


def test_artifact_metrics_describe_the_compiled_playbook():
    version = vendor_version("http://x")
    metrics = artifact_metrics(version)

    # blindfold: contract — counts reflect the playbook, not estimates
    assert metrics["steps"] == 9
    assert metrics["assertions_kept"] == 1  # only the submit step carries an assertion
    assert metrics["inputs"] == 4
    # blindfold: spec — every selector in the vendor playbook is a real id (#ctl00_*)
    assert metrics["durability_avg"] == 2.0
    assert metrics["positional_only"] == 0


def test_the_report_renders_honest_aggregates():
    rows = [
        {
            "id": "good", "metrics": {"steps": 9, "durability_avg": 2.0, "assertions_kept": 1},
            "replay": {"replay_ok": True, "replay_s": 1.4},
            "detection": {"detectable": True, "injected_step": 5, "failed_at": 5,
                          "detection_precise": True, "recovery_ok": True},
        },
        {
            "id": "flaky", "metrics": {"steps": 4, "durability_avg": 0.5, "assertions_kept": 0},
            "replay": {"replay_ok": False, "replay_s": 8.0},
            "detection": {"detectable": True, "injected_step": 2, "failed_at": 7,
                          "detection_precise": False, "recovery_ok": False},
        },
    ]

    report = render_report(rows)

    # blindfold: contract — successes and failures both appear, nothing smoothed over
    assert "deterministic replay success: **1/2**" in report
    assert "fault detection precision: **1/2**" in report
    assert "recovery to green after republish: **1/2**" in report
    assert "$0.00 model cost" in report

    # blindfold: contract — the repair comparison renders as its own section
    rows[0]["repair_strategies"] = {
        "_anchor": {"injected_step": 5},
        "static_floor": {"completed": False, "failed_at": 5, "time_s": 2.0},
        "attribute_similarity": {"completed": True, "failed_at": 5, "repaired_steps": [5], "time_s": 3.1},
    }
    rows[0]["detection"]["recovery_s"] = 14.2
    rows[0]["model"] = {"explore_s": 51.2, "usd_per_call": 0.03}
    comparative = render_report(rows)
    assert "Repair under an identical locator break (1 target)" in comparative
    assert "| static floor (no repair) | 0/1" in comparative
    assert "| attribute similarity (Similo-style) | 1/1" in comparative
    assert "| clickwright — computer-use heal loop | 1/1" in comparative
