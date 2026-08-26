"""The OpenAI-compatible explorer, exercised without a model or a network.

The loop is ours here — ADK's native computer use only exists on Gemini — so the
parts that would otherwise be the provider's problem are the parts worth testing:
which backend gets built, what a tool result looks like on the wire, and what the
conversation costs on the twentieth turn.
"""

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import openai_explorer as backend
from app.agents.backend import build_explorer


def test_the_gemini_path_stays_the_default(monkeypatch):
    """Computer use is native on Gemini and hand-rolled everywhere else, so a
    project with no base URL set must not silently get the hand-rolled one."""
    monkeypatch.delenv("CLICKWRIGHT_MODEL_BASE_URL", raising=False)

    # blindfold: contract — base URL absent means the ADK/Gemini explorer
    assert type(build_explorer()).__name__ == "Explorer"

    monkeypatch.setenv("CLICKWRIGHT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    # blindfold: contract — a base URL means the OpenAI-protocol explorer
    assert type(build_explorer()).__name__ == "OpenAIExplorer"


def test_a_screenshot_rides_on_a_user_message_not_the_tool_result():
    """Chat completions ignore images on tool messages: a screenshot returned
    that way is billed as base64 text and never seen."""
    reply = backend._tool_reply("call_1", {"url": "https://x.test", "screenshot": b"PNG"})

    # blindfold: contract — tool result first, then the frame it produced
    assert [m["role"] for m in reply] == ["tool", "user"]
    assert json.loads(reply[0]["content"]) == {"url": "https://x.test"}
    assert reply[1]["content"][0]["image_url"]["url"] == (
        "data:image/png;base64," + base64.b64encode(b"PNG").decode()
    )


def test_only_the_newest_frames_are_resent():
    """The whole conversation is replayed every turn. Twenty steps of frames is
    twenty thousand input tokens the model cannot act on."""
    messages = [{"role": "system", "content": "..."}]
    for i in range(4):
        messages.extend(backend._tool_reply(f"c{i}", {"url": "u", "screenshot": f"f{i}".encode()}))

    sent = backend._trimmed(messages)

    frames = [m for m in sent if backend._is_frame(m)]
    # blindfold: contract — LIVE_SCREENSHOTS defaults to 2, newest kept
    assert len(frames) == 2
    assert [f["content"][0]["image_url"]["url"].split(",")[1] for f in frames] == [
        base64.b64encode(b"f2").decode(),
        base64.b64encode(b"f3").decode(),
    ]
    # blindfold: contract — the tool results themselves are never dropped
    assert sum(1 for m in sent if m["role"] == "tool") == 4


@pytest.mark.asyncio
async def test_clicking_a_dropdown_tells_the_model_what_is_in_it():
    """A native <select> renders its list outside the page, so a model that
    clicks one sees no change and clicks forever."""
    computer = MagicMock()
    computer.options_at = AsyncMock(return_value=["— select —", "Travel"])
    computer.click_at = AsyncMock(return_value=MagicMock(screenshot=b"PNG", url="https://x.test"))

    result = await backend.OpenAIExplorer()._act(computer, "click", {"x": 5, "y": 6, "intent": "open it"})

    # blindfold: contract — the labels, plus how to choose one
    assert result["select_options"] == ["— select —", "Travel"]
    assert "type" in result["note"]
    # blindfold: contract — the reason lands on the step this action records
    assert computer.next_intent == "open it"


@pytest.mark.asyncio
async def test_an_unknown_action_is_answered_rather_than_raised():
    """A model that invents an action name would otherwise end the run."""
    computer = MagicMock()
    computer.current_state = AsyncMock(return_value=MagicMock(screenshot=b"PNG", url="https://x.test"))

    result = await backend.OpenAIExplorer()._act(computer, "teleport", {})

    # blindfold: contract — the run continues with the screen and the reason
    assert "teleport" in result["error"]
    # blindfold: contract — the reply carries the current screen, from current_state
    assert result["url"] == "https://x.test"


def test_a_missing_key_names_the_variables_to_set(monkeypatch):
    for name in backend.API_KEY_VARS:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError) as caught:
        backend.OpenAIExplorer()._client()

    # blindfold: contract — the error tells the operator which env vars work
    assert "OPENROUTER_API_KEY" in str(caught.value)


def test_a_fenced_json_reply_still_compiles():
    """response_format is a request, not a guarantee: ox-alpha honours the schema
    and then wraps the object in a ```json fence, which json.loads rejects."""
    from app.agents.distiller import _json_object

    fenced = '```json\n{"inputs": [], "steps": [{"index": 1, "action": "click"}]}\n```'

    # blindfold: contract — the object inside the fence, unchanged
    assert _json_object(fenced) == {"inputs": [], "steps": [{"index": 1, "action": "click"}]}
    # blindfold: contract — a bare object passes through untouched
    assert _json_object('{"inputs": [], "steps": []}') == {"inputs": [], "steps": []}


def test_a_reply_with_no_json_says_so():
    from app.agents.distiller import _json_object

    with pytest.raises(ValueError) as caught:
        _json_object("I could not compile that.")

    # blindfold: contract — the error quotes what came back instead of JSON
    assert "could not compile" in str(caught.value)


def test_a_wrapped_playbook_is_unwrapped():
    """Observed from ox-alpha: schema honoured, then buried under a "playbook"
    key with commentary after the fence. Throwing here would discard a browser
    run that already succeeded."""
    from app.agents.distiller import _json_object

    reply = (
        '```json\n{"playbook": {"inputs": [], "steps": [{"index": 1, "action": "click"}]}}\n```\n'
        "\n**Notes**: all focus clicks return null."
    )

    # blindfold: contract — the inner object, fence and commentary discarded
    assert _json_object(reply) == {"inputs": [], "steps": [{"index": 1, "action": "click"}]}


def test_prose_after_the_object_does_not_break_the_parse():
    """The model closes with a sentence. Scanning to the last brace swallows it."""
    from app.agents.distiller import _json_object

    reply = '{"inputs": [], "steps": [{"index": 1, "action": "click"}]}\n\nNotes: {see above}'

    # blindfold: contract — the first complete object, commentary ignored
    assert _json_object(reply) == {"inputs": [], "steps": [{"index": 1, "action": "click"}]}


def test_a_trailing_comma_is_repaired_rather_than_fatal():
    from app.agents.distiller import _json_object

    reply = '{"inputs": [], "steps": [{"index": 1, "action": "click",},],}'

    # blindfold: contract — JSON5-ish sloppiness costs nothing to forgive
    assert _json_object(reply) == {"inputs": [], "steps": [{"index": 1, "action": "click"}]}


def test_unparseable_json_reports_where_it_broke():
    """A bare 'Expecting , delimiter: line 25' tells you nothing about a reply
    you never see. The retry needs the text, and so does the operator."""
    from app.agents.distiller import MalformedPlaybook, _json_object

    reply = '{"steps": [{"index": 1, "expect_text": "he said "hi" loudly"}]}'

    with pytest.raises(MalformedPlaybook) as caught:
        _json_object(reply)

    # blindfold: contract — the exception carries the reply and quotes the break
    assert caught.value.reply == reply
    assert "hi" in str(caught.value)


@pytest.mark.asyncio
async def test_a_failing_action_is_reported_not_raised():
    """Observed live: the model asked for key "Left", Playwright wants
    "ArrowLeft", and the exception ended a twelve-step run."""
    computer = MagicMock()
    computer.key_combination = AsyncMock(side_effect=ValueError('Unknown key: "Left"'))
    computer.current_state = AsyncMock(return_value=MagicMock(screenshot=b"PNG", url="https://x.test"))

    result = await backend.OpenAIExplorer()._act(computer, "key_press", {"keys": ["Left"]})

    # blindfold: contract — the model gets the failure and the screen, and continues
    assert "Unknown key" in result["error"]
    assert result["screenshot"] == b"PNG"


@pytest.mark.asyncio
async def test_a_scope_breach_is_still_fatal():
    from app.computer import hosts

    computer = MagicMock()
    computer.navigate = AsyncMock(side_effect=hosts.HostRefused("evil.test"))

    with pytest.raises(hosts.HostRefused):
        await backend.OpenAIExplorer()._act(computer, "navigate", {"url": "https://evil.test"})


def test_key_names_a_model_uses_map_to_playwright_names():
    from app.computer.playwright_computer import PlaywrightComputer

    aliases = PlaywrightComputer.KEY_ALIASES

    # blindfold: contract — DOM key values, which Playwright requires
    assert aliases["left"] == "ArrowLeft"
    assert aliases["esc"] == "Escape"
    assert aliases["ctrl"] == "Control"
