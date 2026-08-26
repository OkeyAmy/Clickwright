# Plugging a connector into your agent

A connector is one HTTP call. Everything below is the same three steps in a
different dialect: find it, call it, handle the three answers it can give.

Nothing here needs a deployment. If Clickwright is running on your laptop, the
base URL is `http://localhost:8080`.

---

## 1. Find what exists

```bash
curl localhost:8080/api/connectors
```

```json
[{ "id": "wikipedia", "operation": "find-article",
   "path": "/connectors/wikipedia/find-article", "active_version": "1.0.0" }]
```

The call signature lives at `/api/connectors/wikipedia/openapi`, and the same
contract in skill form at `/api/connectors/wikipedia/skill`.

## 2. Call it

```bash
curl -X POST localhost:8080/connectors/wikipedia/find-article \
  -H 'content-type: application/json' \
  -d '{"search_query": "Grace Hopper"}'
```

Credentials are never in that payload. The runtime injects them at the browser.

## 3. Handle the three answers

| Answer | Meaning | What your agent does |
| --- | --- | --- |
| `200 {"status": "ok", "reference": ..., "confirmation": ...}` | It ran | Use `reference` — usually the id the system generated |
| `200 {"status": "held_for_approval", "approval_id": ...}` | A human must decide first | Surface it, poll `/api/approvals`, or wait and retry |
| `502 {"error": ..., "failed_step": N, "run_id": ...}` | Replay broke at step N | The healer repairs it; `GET /api/runs/{run_id}` says what happened |

---

## From an ADK agent

`OpenAPIToolset` reads the document and the connector becomes a tool. No
integration code, and nothing to update when the connector is healed — the
document is generated from the active version.

```python
import httpx
from google.adk import Agent
from google.adk.tools.openapi_tool import OpenAPIToolset

spec = httpx.get("http://localhost:8080/api/connectors/wikipedia/openapi").json()

agent = Agent(
    model="gemini-3.5-flash",
    name="ops",
    instruction="Use the tools you have. They operate systems that have no API.",
    tools=[OpenAPIToolset(spec_dict=spec)],
)
```

Point it at every connector at once by looping `/api/connectors` and building a
toolset per entry — that is what `app/agents/consumer.py` does.

## From the Claude API

```python
import anthropic, httpx

spec = httpx.get("http://localhost:8080/api/connectors/wikipedia/openapi").json()
body = spec["paths"]["/connectors/wikipedia/find-article"]["post"]["requestBody"]
schema = body["content"]["application/json"]["schema"]

client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-sonnet-5",
    max_tokens=1024,
    tools=[{
        "name": "find_article",
        "description": "Look up an article on Wikipedia, a system this agent drives through its UI.",
        "input_schema": schema,
    }],
    messages=[{"role": "user", "content": "Find the article on Grace Hopper."}],
)

for block in response.content:
    if block.type == "tool_use":
        result = httpx.post(
            "http://localhost:8080/connectors/wikipedia/find-article",
            json=block.input, timeout=120,
        ).json()
```

## From anything else

It is an HTTP POST with a JSON body. LangChain, a cron job, a Zapier webhook, a
shell script — all the same call. The OpenAPI document means most frameworks
can generate the client for you.

---

## What a run does when it needs you

An exploration is not a black box that either finishes or fails. It stops and
asks in two situations:

- **It is about to do something irreversible** — submit, send, delete, book.
  The run pauses, and the pending item at `/api/approvals` carries the reason,
  the control it is about to operate, and a screenshot of what it is looking
  at. `POST /api/approvals/{id}/approve` lets it continue; `deny` tells it to
  finish without that action.
- **It needs something only you have** — a code texted to your phone, a code
  from an authenticator, a choice between options, a detail the task never
  gave. The run pauses with a question. `POST /api/approvals/{id}/answer` with
  `{"value": "482913"}` hands it over.

A sensitive answer is handled like a password: it goes into the browser's
secret table and the agent is told to type `{{answer_1}}`. The value never
enters the model's context, never appears in the screenshots it sees next, and
is never written to the audit trail — which keeps the question and the fact it
was answered, not the answer.

Nobody answering is a refusal, not a hang: after ten minutes the run is told the
action was declined, and it reports what it left undone.
