# Calling Jev Ultrafast from n8n

`jev-service` exposes one browser run at a time over HTTP so an n8n **AI Agent** node can use it as a tool. When a
field's value is not covered by the goal, the run pauses and hands the question back to the agent instead of guessing.

The service runs on the machine where Chrome runs — not inside the n8n container. Chrome uses your existing profile, so
the agent acts as the signed-in user.

## Start the service

```bash
uv run --env-file .env jev-service
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `JEV_SERVICE_TOKEN` | — | Required. Bearer token; the service refuses to start without it. |
| `JEV_SERVICE_HOST` | `127.0.0.1` | Set to `0.0.0.0` to accept connections from containers. |
| `JEV_SERVICE_PORT` | `8767` | The demo inspector uses 8766. |
| `JEV_CALL_TIMEOUT` | `120` | Seconds a single call may drive the run before it is stopped and closed. |
| `JEV_RUN_TTL` | `300` | Seconds a paused run waits for an answer before its tab is closed. |

`TYPESAFE_API_KEY` and `TEXT_MODEL_API_KEY` are still required, exactly as for the demo.

Binding to `0.0.0.0` exposes the port to your whole network, not only to Docker. Keep the token secret and keep a host
firewall in place.

## Reach it from the n8n container

On Docker Desktop for macOS and Windows, `host.docker.internal` already resolves to the host. On Linux, add the mapping:

```yaml
services:
  n8n:
    image: docker.n8n.io/n8nio/n8n
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

Verify from inside the container before wiring anything:

```bash
docker compose exec n8n \
  wget -qO- --header="Authorization: Bearer $JEV_SERVICE_TOKEN" http://host.docker.internal:8767/health
```

## Credential

Create a **Header Auth** credential: name `Authorization`, value `Bearer <your token>`. Both tools use it.

## Tool 1 — start a run

Add an **HTTP Request Tool** connected to the AI Agent's tool port.

- Name: `browser_fill_form`
- Method: `POST`, URL: `http://host.docker.internal:8767/run`
- Timeout: above `JEV_CALL_TIMEOUT`, or the node gives up while the browser keeps working
- Description: *Opens a URL in a real signed-in browser and completes a form. The goal must contain every value to
  enter. Returns `awaiting_answer` when the form asks something the goal does not cover.*
- JSON body:

```json
{
  "url": "{{ $fromAI('url', 'form page URL to open', 'string') }}",
  "goal": "{{ $fromAI('goal', 'complete task, including every value to enter', 'string') }}"
}
```

## Tool 2 — answer an escalated field

- Name: `browser_answer_question`
- Method: `POST`, URL: `http://host.docker.internal:8767/answer`
- Description: *Supplies the value for the field in the previous `awaiting_answer` response and resumes that run.*
- JSON body:

```json
{
  "run_id": "{{ $fromAI('run_id', 'run_id from the awaiting_answer response', 'string') }}",
  "text": "{{ $fromAI('text', 'exact value to type into question.field.label', 'string') }}"
}
```

## Agent system prompt

Add this to the AI Agent node's system message:

> When `browser_fill_form` returns `status: "awaiting_answer"`, read `question.field.label` and `question.page.text`,
> decide the value, and call `browser_answer_question` with the same `run_id`. Repeat until the status is `done`,
> `blocked`, or `timeout`. Verify the outcome in `page_text`; a `done` status alone is not proof of success.

## Responses

Both tools return the same envelope:

```json
{
  "run_id": "r_8f3…",
  "status": "awaiting_answer",
  "steps": 7,
  "elapsed_ms": 8210,
  "final_url": "https://forms.example.com/apply?step=3",
  "page_text": "…first 4000 characters of the visible page…",
  "question": {
    "field": {"label": "Why are you applying?", "role": "textbox", "value": ""},
    "page": {"title": "Application", "text": "…first 4000 characters…"},
    "recent_actions": [{"action": "Full name", "text": "Ada Lovelace"}]
  }
}
```

`question` appears only with `awaiting_answer`. Statuses are `done`, `blocked`, `awaiting_answer`, and `timeout`.

`timeout` is a stop, not a pause: the run exceeded `JEV_CALL_TIMEOUT`, the tab was closed, and the form may be left
partly filled. `page_text` shows where it stopped.

## Failures

| Status | Meaning |
| --- | --- |
| `400` | Bad body, non-`http(s)` URL, or a goal outside 1–2,000 characters. |
| `401` | Missing or wrong bearer token. |
| `409` | A run is already active — the body carries its `run_id`. Answer it, or `POST /cancel` with that id. |
| `502` | The model provider failed. No browser action was executed and the run was closed. |
| `500` | The run failed and was closed. Nothing is retried automatically. |

`POST /cancel` with `{"run_id": "…"}` closes a run and frees the slot. `GET /health` reports the active `run_id`.

## Scope

`url` is caller-supplied and unrestricted, and the browser carries your signed-in Chrome profile. Any text the n8n agent
read upstream — an email body, a ticket, a scraped page — can therefore steer an authenticated browser to an arbitrary
origin and type into it. The bearer token, the loopback default, and the run TTL limit that exposure; they do not remove
it. Restrict the origins the service accepts if the workflow reads untrusted input.

Only one run exists at a time, so parallel n8n executions receive `409` rather than sharing a browser.
