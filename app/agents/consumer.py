"""Consumer — an agent that uses connectors it did not compile.

This is what makes the registry a fleet rather than a filing cabinet. This agent
has never operated a browser. It discovers connectors, loads their OpenAPI
documents through ADK's OpenAPIToolset, and calls them like any other tool.

The base URL comes from the spec's `servers` block — OpenAPIToolset has no
parameter for it, which is why the registry emits it.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from google.adk import Agent
from google.adk.runners import InMemoryRunner
from google.adk.tools.openapi_tool import OpenAPIToolset
from google.genai import types

from app.connectors.registry import Registry

MODEL = os.getenv("CLICKWRIGHT_CONSUMER_MODEL", "gemini-3.5-flash")

INSTRUCTION = """\
You complete back-office tasks by calling connectors that other agents compiled \
from systems with no API.

- Choose the connector whose description matches the task. If none does, say so \
  rather than improvising.
- Pass exactly the values the task supplies. Never invent identifiers, amounts \
  or names.
- Some calls come back held for human approval. That is a normal outcome, not an \
  error: report it plainly and stop.
- Report each result with the reference the connector returned.
"""


class Consumer:
    def __init__(self, registry: Optional[Registry] = None, server_url: Optional[str] = None):
        self.registry = registry or Registry()
        self.server_url = server_url or os.getenv("CLICKWRIGHT_RUNTIME_URL", "http://localhost:8080")

    def toolsets(self) -> list[OpenAPIToolset]:
        """One toolset per published connector, discovered at runtime."""
        toolsets = []
        for connector in self.registry.list():
            if not connector.active():
                continue
            spec = self.registry.openapi(connector, self.server_url)
            toolsets.append(
                OpenAPIToolset(
                    spec_str=json.dumps(spec),
                    spec_str_type="json",
                    tool_name_prefix=connector.id.replace("-", "_"),
                )
            )
        return toolsets

    def build_agent(self) -> Agent:
        return Agent(
            model=MODEL,
            name="ops_agent",
            description="Completes back-office tasks through published connectors.",
            instruction=INSTRUCTION,
            tools=self.toolsets(),
        )

    async def run(self, task: str) -> str:
        runner = InMemoryRunner(agent=self.build_agent(), app_name="clickwright")
        session = await runner.session_service.create_session(
            app_name="clickwright", user_id="ops"
        )
        out = []
        async for event in runner.run_async(
            user_id="ops",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=task)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                out.append("".join(p.text or "" for p in event.content.parts))
        return "\n".join(out).strip()
