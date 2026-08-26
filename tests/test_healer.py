"""The healer must never publish a fix that is not one.

A recovery run that lands on the same selector as the step that broke used to
be compiled into a "new" version identical to the old one — new number, same
bug — so every canary failed again forever. These tests pin the guard, the
unreachable-target rule, and the heal strategies.
"""

from unittest.mock import MagicMock

from app.agents.distiller import Distiller
from app.agents.healer import HealResult, Healer
from app.connectors.models import (
    Action,
    ConnectorVersion,
    PlaybookStep,
    RunRecord,
    Selector,
    Trajectory,
    TrajectoryStep,
)
from app.connectors.registry import Registry
from tests.factories import vendor_connector


class StubExplorer:
    def __init__(self, trajectory: Trajectory):
        self.trajectory = trajectory
        self.calls: list[dict] = []

    async def explore(self, **kwargs):
        self.calls.append(kwargs)
        return self.trajectory


class StubRuntime:
    def __init__(self, run: RunRecord):
        self.run = run

    async def execute(self, *args, **kwargs):
        return self.run


class StubDistiller:
    """Records which compile path the healer chose; never touches a model."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def recompile_step(self, *, previous, trajectory, failed_index, version, reason):
        self.calls.append(("step", version))
        return previous.model_copy(
            deep=True,
            update={
                "version": version,
                "healed_from": previous.version,
                "steps": [
                    PlaybookStep(index=failed_index, action=Action.CLICK,
                                 selector=Selector(primary="#patched"))
                ],
                "inputs": [f.model_copy() for f in previous.inputs],
            },
        )

    def compile(self, trajectory, version, healed_from=None, heal_reason=None):
        self.calls.append(("full", version))
        return ConnectorVersion(
            version=version,
            healed_from=healed_from,
            heal_reason=heal_reason,
            steps=[
                PlaybookStep(index=s.index, action=s.action, selector=s.selector,
                             url=s.url if s.action == Action.NAVIGATE else None)
                for s in trajectory.steps
            ],
            inputs=[],
        )


def _healer(registry, trajectory: Trajectory, distiller=None, runtime=None) -> Healer:
    return Healer(
        registry=registry,
        runtime=runtime or MagicMock(),
        explorer=StubExplorer(trajectory),
        distiller=distiller or Distiller(client=MagicMock()),
        artifacts_dir=None,
    )


def _failed_run(failed_step: int) -> RunRecord:
    return RunRecord(
        id="run_canary",
        connector_id="vendor-portal",
        mode="canary",
        version="1.0.0",
        status="failed",
        failed_step=failed_step,
        error="no selector matched (2 candidates tried): #gone",
    )


def _heal_result(failed_step: int) -> HealResult:
    """What Healer.check() returns for the failed canary above."""
    run = _failed_run(failed_step)
    return HealResult("vendor-portal", healthy=False, run=run, reason=run.error)


def _recovery(selector_primary: str) -> Trajectory:
    return Trajectory(
        run_id="run_heal",
        connector_id="vendor-portal",
        goal="recover",
        model="m",
        steps=[
            TrajectoryStep(index=1, action=Action.NAVIGATE, url="http://localhost:8081/vendor/login"),
            TrajectoryStep(index=2, action=Action.CLICK, selector=Selector(primary=selector_primary)),
        ],
    )


def _registry_with_active_step(registry_home, selector_primary: str) -> Registry:
    registry = Registry(registry_home / "registry")
    connector = vendor_connector("http://localhost:8081")
    registry.publish(
        connector,
        ConnectorVersion(
            version="1.0.0",
            steps=[PlaybookStep(index=2, action=Action.CLICK, selector=Selector(primary=selector_primary))],
            inputs=[],
        ),
    )
    return registry


async def test_a_recovery_that_changes_nothing_is_not_published(registry_home):
    """Same selector in, same selector out — publishing would only burn a canary."""
    registry = _registry_with_active_step(registry_home, "#same")

    healer = _healer(registry, _recovery("#same"))
    result = await healer.heal(registry.get("vendor-portal"), _heal_result(2))

    # blindfold: contract — no change means no publish; healed_to stays None
    assert result.healed_to is None
    assert "no change" in (result.reason or "")
    # blindfold: invariant — the registry still holds exactly one version
    assert [v.version for v in registry.get("vendor-portal").versions] == ["1.0.0"]


async def test_a_real_fix_publishes_the_next_version(registry_home):
    registry = _registry_with_active_step(registry_home, "#stale")

    healer = _healer(registry, _recovery('button[data-testid="fresh"]'))
    result = await healer.heal(registry.get("vendor-portal"), _heal_result(2))

    # blindfold: contract — a changed step is published as the next minor
    assert result.healed_to == "1.1.0"
    active = registry.get("vendor-portal").active()
    assert active.steps[0].selector.primary == 'button[data-testid="fresh"]'
    assert active.healed_from == "1.0.0"


async def test_the_healer_starts_where_the_run_stopped_trusting_the_page(registry_home):
    """The runtime records final_url when a step fails; replaying navigation the
    earlier steps already completed would waste the recovery run's budget."""
    registry = _registry_with_active_step(registry_home, "#x")
    explorer = StubExplorer(_recovery("#x"))
    healer = Healer(
        registry=registry,
        runtime=MagicMock(),
        explorer=explorer,
        distiller=Distiller(client=MagicMock()),
        artifacts_dir=None,
    )
    result = _heal_result(2)
    result.run.result = {"final_url": "http://localhost:8081/vendor/new-claim"}

    await healer.heal(registry.get("vendor-portal"), result)

    # blindfold: contract — explore() receives the failure-point URL as start_url
    assert explorer.calls, "the recovery exploration never ran"
    assert explorer.calls[0]["start_url"] == "http://localhost:8081/vendor/new-claim"


async def test_an_unreachable_target_is_reported_not_healed():
    """A DNS/connection failure with zero executed steps says nothing about the
    playbook. Healing it would spend a model run to rediscover the dead wire."""
    run = RunRecord(
        id="run_dead",
        connector_id="vendor-portal",
        mode="canary",
        version="1.0.0",
        status="failed",
        error='Error: Page.goto: net::ERR_NAME_NOT_RESOLVED at https://anywhere.test/',
    )
    healer = Healer(
        registry=MagicMock(),
        runtime=StubRuntime(run),
        explorer=MagicMock(),
        distiller=MagicMock(),
        artifacts_dir=None,
    )

    result = await healer.check(MagicMock(id="vendor-portal"))

    # blindfold: contract — unreachable means unhealthy and explicitly not healable
    assert result.healthy is False
    assert result.healable is False
    assert "unreachable" in (result.reason or "")


async def test_a_playbook_failure_on_a_loaded_page_is_still_healable():
    """Any executed step proves the target answered — the failure belongs to
    the playbook, so the heal path stays available."""
    run = RunRecord(
        id="run_drifted",
        connector_id="vendor-portal",
        mode="canary",
        version="1.0.0",
        status="failed",
        failed_step=9,
        error="no selector matched (2 candidates tried): #ctl00_submit",
        steps=[TrajectoryStep(index=1, action=Action.NAVIGATE, url="http://localhost:8081/vendor/login")],
    )
    healer = Healer(
        registry=MagicMock(),
        runtime=StubRuntime(run),
        explorer=MagicMock(),
        distiller=MagicMock(),
        artifacts_dir=None,
    )

    result = await healer.check(MagicMock(id="vendor-portal"))

    assert result.healthy is False
    # blindfold: contract — a real drift is healable even when the error text mentions net:: elsewhere
    assert result.healable is True


async def test_auto_escalates_to_a_full_rebuild_when_a_healed_version_fails_again(registry_home, monkeypatch):
    monkeypatch.delenv("CLICKWRIGHT_HEAL_STRATEGY", raising=False)
    registry = _registry_with_active_step(registry_home, "#stale")
    stored = registry.get("vendor-portal")
    stored.versions[0].healed_from = "0.9.0"  # this active version is itself a heal
    registry.save(stored)

    distiller = StubDistiller()
    explorer = StubExplorer(_recovery("#fresh"))
    healer = Healer(registry=registry, runtime=MagicMock(), explorer=explorer,
                    distiller=distiller, artifacts_dir=None)

    result = await healer.heal(registry.get("vendor-portal"), _heal_result(2))

    # blindfold: contract — auto escalates to the full compile path
    assert distiller.calls == [("full", "1.1.0")]
    published = registry.get("vendor-portal").active()
    # blindfold: contract — the rebuild comes from the whole traversal, not one patched step
    assert [s.index for s in published.steps] == [1, 2]
    assert result.healed_to == "1.1.0"


async def test_the_step_strategy_keeps_patching_even_a_healed_version(registry_home, monkeypatch):
    monkeypatch.setenv("CLICKWRIGHT_HEAL_STRATEGY", "step")
    registry = _registry_with_active_step(registry_home, "#stale")
    stored = registry.get("vendor-portal")
    stored.versions[0].healed_from = "0.9.0"
    registry.save(stored)

    distiller = StubDistiller()
    healer = Healer(registry=registry, runtime=MagicMock(), explorer=StubExplorer(_recovery("#x")),
                    distiller=distiller, artifacts_dir=None)

    await healer.heal(registry.get("vendor-portal"), _heal_result(2))

    # blindfold: contract — an explicit strategy beats the auto escalation
    assert distiller.calls == [("step", "1.1.0")]


async def test_a_twin_of_the_currently_published_head_is_not_published_again(registry_home):
    """Two heals racing off the same snapshot both recompile to the same steps.
    The loser must not publish 1.x.0 over identical content."""
    registry = _registry_with_active_step(registry_home, "#stale")
    stale_snapshot = registry.get("vendor-portal")  # what both racers read

    first = await _healer(registry, _recovery("#new")).heal(stale_snapshot, _heal_result(2))
    assert first.healed_to == "1.1.0"

    second = await _healer(registry, _recovery("#new")).heal(stale_snapshot, _heal_result(2))

    # blindfold: contract — the racing twin is refused against the current head
    assert second.healed_to is None
    assert [v.version for v in registry.get("vendor-portal").versions] == ["1.1.0", "1.0.0"]
