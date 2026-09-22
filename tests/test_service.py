"""Offline contracts for the machine-facing service. No paid APIs, no browser."""

import json
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from jev_ultrafast import service

QUESTION = {
    "field": {"label": "Why are you applying?", "role": "textbox", "value": ""},
    "page": {"title": "Form", "text": "q" * 9000},
    "recent_actions": [],
    "goal": "Apply for the role",
}


class StubAgent:
    """Replays a scripted status sequence in place of a real browser run."""

    def __init__(self, url, goal, script):
        self.url, self.goal, self.script = url, goal, list(script)
        self.closed = False
        self.answers = []
        self.state = {
            "page": {"url": url, "title": "Form", "text": "p" * 9000, "actions": [], "fingerprint": "f"},
            "status": "ready",
            "history": [],
            "elapsed_ms": 12,
            "question": None,
        }

    def snapshot(self):
        return dict(self.state)

    def command(self, name, body=None):
        if name == "answer":
            self.answers.append((body or {}).get("text"))
        self.state["status"] = self.script.pop(0) if self.script else "done"
        if self.state["status"] == service.PAUSED:
            self.state["question"] = dict(QUESTION)
        else:
            self.state["question"] = None
            self.state["history"].append({"step": len(self.state["history"]) + 1})
        return self.snapshot()

    def close(self):
        self.closed = True


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(service, "TOKEN", "secret")
    monkeypatch.setattr(service, "CALL_TIMEOUT", 5.0)
    monkeypatch.setattr(service, "RUN_TTL", 300.0)
    monkeypatch.setattr(service, "RUN", None)
    built = SimpleNamespace(script=["done"], agents=[])

    def build(url, goal):
        agent = StubAgent(url, goal, built.script)
        built.agents.append(agent)
        return agent

    monkeypatch.setattr(service, "Agent", build)
    server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    built.port = server.server_address[1]
    yield built
    server.shutdown()
    server.server_close()
    service.RUN = None


def call(port, method, path, body=None, token="secret"):
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    connection.request(method, path, json.dumps(body) if body is not None else None, headers)
    response = connection.getresponse()
    payload = json.loads(response.read() or b"{}")
    connection.close()
    return response.status, payload


def apply_run(port, goal="Apply for the role"):
    return call(port, "POST", "/run", {"url": "https://forms.test/apply", "goal": goal})


@pytest.mark.parametrize("token", [None, "wrong"])
@pytest.mark.parametrize(("method", "path"), [("GET", "/health"), ("POST", "/run")])
def test_bearer_token_is_required(api, token, method, path):
    status, _ = call(api.port, method, path, {"url": "https://forms.test/", "goal": "x"}, token=token)
    assert status == 401
    assert api.agents == []


def test_unknown_route_is_rejected(api):
    assert call(api.port, "POST", "/execute", {})[0] == 404
    assert call(api.port, "GET", "/state")[0] == 404


def test_finished_run_returns_capped_evidence_and_closes(api):
    status, body = apply_run(api.port)
    assert status == 200
    assert body["status"] == "done" and body["steps"] == 1 and body["elapsed_ms"] == 12
    assert body["final_url"] == "https://forms.test/apply"
    assert len(body["page_text"]) == service.PAGE_TEXT_LIMIT
    assert "question" not in body
    assert api.agents[0].closed and service.RUN is None


def test_uncovered_field_returns_the_question_and_keeps_the_run_open(api):
    api.script = [service.PAUSED]
    status, body = apply_run(api.port)
    assert status == 200 and body["status"] == service.PAUSED
    assert body["question"]["field"]["label"] == "Why are you applying?"
    assert len(body["question"]["page"]["text"]) == service.PAGE_TEXT_LIMIT
    assert "goal" not in body["question"]
    assert not api.agents[0].closed
    assert call(api.port, "GET", "/health")[1]["run_id"] == body["run_id"]


def test_answer_resumes_the_same_run(api):
    api.script = [service.PAUSED, "done"]
    _, paused = apply_run(api.port)
    status, body = call(api.port, "POST", "/answer", {"run_id": paused["run_id"], "text": "Because I can"})
    assert status == 200 and body["status"] == "done" and body["run_id"] == paused["run_id"]
    assert api.agents[0].answers == ["Because I can"]
    assert api.agents[0].closed and service.RUN is None


def test_second_run_is_refused_with_the_active_run_id(api):
    api.script = [service.PAUSED]
    _, paused = apply_run(api.port)
    status, body = apply_run(api.port)
    assert status == 409 and body["run_id"] == paused["run_id"]
    assert len(api.agents) == 1


@pytest.mark.parametrize("run_id", ["r_wrong", None])
def test_answer_requires_the_matching_run_id(api, run_id):
    api.script = [service.PAUSED]
    apply_run(api.port)
    status, _ = call(api.port, "POST", "/answer", {"run_id": run_id, "text": "Because I can"})
    assert status == 409
    assert api.agents[0].answers == []
    assert not api.agents[0].closed


def test_cancel_frees_the_slot(api):
    api.script = [service.PAUSED]
    _, paused = apply_run(api.port)
    status, body = call(api.port, "POST", "/cancel", {"run_id": paused["run_id"]})
    assert status == 200 and body["status"] == "cancelled"
    assert api.agents[0].closed and service.RUN is None


@pytest.mark.parametrize(
    "body",
    [
        {"url": "https://forms.test/", "goal": ""},
        {"url": "https://forms.test/", "goal": "   "},
        {"url": "https://forms.test/", "goal": "x" * 2001},
        {"url": "https://forms.test/"},
        {"url": "ftp://forms.test/", "goal": "Apply"},
        {"url": "forms.test", "goal": "Apply"},
        {"goal": "Apply"},
    ],
)
def test_invalid_start_requests_are_rejected_before_the_browser_opens(api, body):
    status, _ = call(api.port, "POST", "/run", body)
    assert status == 400
    assert api.agents == []


def test_call_budget_stops_and_closes_the_run(api, monkeypatch):
    monkeypatch.setattr(service, "CALL_TIMEOUT", -1.0)
    api.script = ["ready"]
    status, body = apply_run(api.port)
    assert status == 200 and body["status"] == "timeout"
    assert body["page_text"]
    assert api.agents[0].closed and service.RUN is None


def test_abandoned_paused_run_is_reaped(api, monkeypatch):
    api.script = [service.PAUSED]
    apply_run(api.port)
    monkeypatch.setattr(service, "RUN_TTL", -1.0)
    service.reap()
    assert api.agents[0].closed and service.RUN is None


def test_provider_failure_closes_the_run(api, monkeypatch):
    def build(url, goal):
        raise RuntimeError("Model connection failed; no action executed.")

    monkeypatch.setattr(service, "Agent", build)
    status, body = apply_run(api.port)
    assert status == 502 and "no action executed" in body["error"]
    assert service.RUN is None
