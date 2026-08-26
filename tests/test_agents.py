"""The agents need a model to *run*, but they must always be assemblable.

These tests catch the failures that only show up at wiring time — a renamed ADK
API, a toolset that rejects the model id, a placeholder that stops resolving —
without spending a single token.
"""

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import types

from app.agents.consumer import Consumer
from app.agents.explorer import Explorer
from app.computer.playwright_computer import PlaywrightComputer


def _request_with_screenshots(count: int) -> MagicMock:
    request = MagicMock()
    request.config.tools = []
    request.contents = [
        types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="click_at",
                        response={
                            "image": {
                                "mimetype": "image/png",
                                "data": base64.b64encode(f"frame-{i}".encode()).decode(),
                            },
                            "url": f"https://x.test/{i}",
                        },
                    )
                )
            ],
        )
        for i in range(count)
    ]
    return request


def test_screenshots_travel_as_images_not_as_base64_text():
    """Measured on the bundled portal: a frame inside the tool-response JSON costs
    ~14k input tokens, the same PNG as an image part ~1.1k. ADK resends the whole
    conversation each turn, so the JSON form exhausts the free-tier quota by step 8."""
    explorer = Explorer()
    request = _request_with_screenshots(5)

    explorer._before_model(MagicMock(), request)

    images = [
        part.inline_data.data
        for content in request.contents
        for part in content.parts
        if getattr(part, "inline_data", None)
    ]
    # blindfold: contract — CLICKWRIGHT_LIVE_SCREENSHOTS defaults to 2, newest kept
    assert images == [b"frame-3", b"frame-4"]
    # blindfold: contract — no response may still carry a base64 image
    assert not any(
        "image" in part.function_response.response
        for content in request.contents
        for part in content.parts
        if getattr(part, "function_response", None)
    )
    dropped = request.contents[0].parts[0].function_response.response
    assert dropped["url"] == "https://x.test/0" and "note" in dropped


def test_the_explorer_assembles_on_a_gemini_3_5_model():
    """ADK's published sample pins gemini-2.5-computer-use-preview, which would
    breach the challenge's Gemini 3.5+ floor. The toolset is model-agnostic —
    this is the test that keeps that true."""
    explorer = Explorer()
    computer = PlaywrightComputer(allowed_hosts=["example.com"])

    agent = explorer.build_agent(computer, private_target=False)

    # blindfold: contract — CLICKWRIGHT_EXPLORER_MODEL defaults to gemini-3.5-flash
    assert agent.model.model == "gemini-3.5-flash"
    assert not agent.model.model.startswith("gemini-2.")
    # blindfold: contract — 429 is the free tier's normal state, not a run-ending error
    assert 429 in agent.model.retry_options.http_status_codes
    # blindfold: contract — computer use is exposed through exactly one ADK toolset
    assert type(agent.tools[0]).__name__ == "ComputerUseToolset"


def test_private_network_access_is_off_for_a_public_target():
    """ADK refuses private addresses by default. Relaxing that for a public
    target would turn the explorer into an SSRF primitive."""
    explorer = Explorer()

    public = explorer.build_agent(PlaywrightComputer(), private_target=False)
    local = explorer.build_agent(PlaywrightComputer(), private_target=True)

    assert public.tools[0]._allow_private_network_access is False
    assert local.tools[0]._allow_private_network_access is True


@pytest.mark.asyncio
async def test_the_instruction_survives_adks_state_substitution():
    """A plain-string instruction is run through ADK's session-state renderer,
    whose pattern matches `{{username}}` and raises KeyError before the first
    screenshot. An InstructionProvider bypasses it."""
    agent = Explorer().build_agent(PlaywrightComputer(), private_target=False)

    instruction, bypass_state_injection = await agent.canonical_instruction(None)

    # blindfold: contract — ADK skips injection only for provider-based instructions
    assert bypass_state_injection is True
    assert "{{username}}" in instruction and "{{password}}" in instruction


def _dispatch(agent, function_call, tools_dict=None):
    """Run ADK's own tool dispatcher, the code that raises on an unknown name."""
    from google.adk.flows.llm_flows import functions

    ctx = MagicMock()
    ctx.invocation_id = "inv"
    ctx.branch = None
    ctx.agent.name = agent.name
    tools_dict = tools_dict or {}
    ctx.plugin_manager.run_on_tool_error_callback = AsyncMock(return_value=None)
    ctx.plugin_manager.run_before_tool_callback = AsyncMock(return_value=None)
    ctx.plugin_manager.run_after_tool_callback = AsyncMock(return_value=None)
    return functions._execute_single_function_call_async(
        ctx, function_call, tools_dict, agent
    )


@pytest.mark.asyncio
async def test_an_invented_tool_name_does_not_kill_the_run():
    """The model reaches for `take_screenshot`, which no computer-use toolset
    has. ADK's dispatcher raises ValueError, which ends the exploration mid-way
    and loses every step recorded so far."""
    computer = MagicMock()
    computer.current_state = AsyncMock(
        return_value=MagicMock(screenshot=b"PNG", url="https://x.test/a")
    )
    agent = Explorer().build_agent(computer, private_target=False)

    event = await _dispatch(agent, types.FunctionCall(name="take_screenshot", args={}))

    response = event.content.parts[0].function_response.response
    # blindfold: contract — the model gets the screen it asked for, plus the reason
    assert response["url"] == "https://x.test/a"
    assert "take_screenshot" in response["error"]
    assert response["image"]["data"] == base64.b64encode(b"PNG").decode()


@pytest.mark.asyncio
async def test_a_host_scope_breach_still_ends_the_run():
    """Recovering from a refused host would let the agent keep probing off-scope
    hosts, one screenshot at a time."""
    from app.computer import hosts

    computer = MagicMock()
    computer.current_state = AsyncMock(
        return_value=MagicMock(screenshot=b"PNG", url="https://x.test/a")
    )
    agent = Explorer().build_agent(computer, private_target=False)

    navigate = MagicMock(name="navigate")
    navigate.name = "navigate"
    navigate.run_async = AsyncMock(side_effect=hosts.HostRefused("evil.test"))

    with pytest.raises(hosts.HostRefused):
        await _dispatch(
            agent,
            types.FunctionCall(name="navigate", args={"url": "https://evil.test"}),
            {"navigate": navigate},
        )


def _stub_computer(options: list[str] | None = None) -> MagicMock:
    computer = MagicMock()
    computer._screen_size = (1000, 1000)
    state = MagicMock(screenshot=b"PNG", url="https://x.test")
    computer.click_at = AsyncMock(return_value=state)
    computer.options_at = AsyncMock(return_value=options or [])
    return computer


def _tool_request(agent) -> MagicMock:
    """Run the before-model callback and hand back the dispatch table it filled."""
    request = MagicMock()
    request.config.tools = []
    request.contents = []
    request.tools_dict = {}
    agent.before_model_callback(callback_context=MagicMock(), llm_request=request)
    return request


@pytest.mark.asyncio
async def test_clicking_a_dropdown_returns_its_options():
    """Chromium draws a native <select> outside the page, so its open list is not
    in any screenshot. The model clicks where the options look like they are,
    sees no change, and repeats — the loop that ate a whole run's quota."""
    computer = _stub_computer(options=["— select —", "Travel", "Meals"])
    agent = Explorer().build_agent(computer, private_target=False)

    response = await _tool_request(agent).tools_dict["click"].run_async(
        args={"x": 500, "y": 150}, tool_context=MagicMock()
    )

    # blindfold: contract — the labels the model cannot see, and what to do with them
    assert response["select_options"] == ["— select —", "Travel", "Meals"]
    assert "type" in response["note"]


@pytest.mark.asyncio
async def test_the_action_names_a_3_5_model_actually_emits_are_dispatchable():
    """ADK 2.7.1 exposes the 2.5 vocabulary — click_at, type_text_at,
    current_state. Observed against gemini-3.5-flash-lite: it asks for `click`
    and `take_screenshot`, gets "tool not found", and retries the same name
    until the quota runs out. That is what the sign-in loop was."""
    computer = _stub_computer()
    agent = Explorer().build_agent(computer, private_target=False)
    request = _tool_request(agent)

    # blindfold: contract — the names the model emits, bound to the 2.5 methods
    assert {"click", "hover", "type_text", "take_screenshot"} <= set(request.tools_dict)
    await request.tools_dict["click"].run_async(
        args={"x": 454, "y": 101, "intent": "Click on User ID input field"},
        tool_context=MagicMock(),
    )
    computer.click_at.assert_awaited_once_with(454, 101)


def test_steps_reach_the_console_while_the_run_is_still_going():
    """Publishing only after the compile leaves the browser pane empty for the
    whole run — and permanently empty if the run dies part-way."""
    from app.connectors.models import Action, TrajectoryStep

    explorer = Explorer()
    computer = MagicMock()
    computer.steps = [
        TrajectoryStep(index=1, action=Action.NAVIGATE, url="https://x.test"),
        TrajectoryStep(index=2, action=Action.TYPE, value="ada@x.test"),
    ]
    seen: list[tuple[str, int, str | None]] = []

    published = explorer._publish_new_steps(
        computer, "run_abc", 0, lambda run_id, s: seen.append((run_id, s.index, s.value))
    )
    computer.steps.append(TrajectoryStep(index=3, action=Action.CLICK))
    explorer._publish_new_steps(
        computer, "run_abc", published, lambda run_id, s: seen.append((run_id, s.index, s.value))
    )

    # blindfold: contract — each step goes out once, redacted, tagged with its run
    assert seen == [("run_abc", 1, None), ("run_abc", 2, "[email]"), ("run_abc", 3, None)]


def test_a_quota_failure_reads_as_one_sentence():
    """The 429 arrives as a page of nested JSON; the console shows one line."""
    from app.server import _explain

    raw = (
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
        "current quota... Quota exceeded for metric: generativelanguage.googleapis.com/"
        "generate_content_free_tier_input_token_count, limit: 250000, model: "
        "gemini-3.5-flash-lite\\nPlease retry in 29.526196846s.'}}"
    )

    expected = (  # blindfold: contract — names the model, rounds the wait, drops the JSON
        "Gemini API quota exhausted (gemini-3.5-flash-lite). Retry in about 30s."
    )
    assert _explain(RuntimeError(raw)) == expected


def test_a_credential_placeholder_resolves_to_the_secret_but_records_the_token():
    computer = PlaywrightComputer(secrets={"password": "s3cret", "username": "alice"})

    # blindfold: contract — (placeholder_kept_for_the_record, text_actually_typed)
    assert computer._resolve_placeholder("{{password}}") == ("{{password}}", "s3cret")
    assert computer._resolve_placeholder("{{username}}") == ("{{username}}", "alice")


def test_an_unknown_placeholder_is_typed_literally_not_silently_dropped():
    """Typing an empty string where a token was meant would produce a playbook
    that looks fine and submits blank fields."""
    computer = PlaywrightComputer(secrets={"password": "s3cret"})

    # blindfold: contract — only known secret names are substituted; the rest pass through
    assert computer._resolve_placeholder("{{api_key}}") == (None, "{{api_key}}")
    assert computer._resolve_placeholder("Travel") == (None, "Travel")


@pytest.mark.asyncio
async def test_the_agent_cannot_leave_the_target_to_search():
    from app.computer import hosts

    with pytest.raises(hosts.HostRefused):
        await PlaywrightComputer(allowed_hosts=["example.com"]).search()


def test_the_consumer_reads_the_registry_rather_than_a_hardcoded_list(registry_home):
    """With an empty registry it must offer no tools at all — an agent that
    invents a connector it cannot see is worse than one that says it can't."""
    from app.connectors.registry import Registry

    consumer = Consumer(registry=Registry(registry_home / "registry"))

    assert consumer.toolsets() == []
