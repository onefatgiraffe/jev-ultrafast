"""Machine-facing HTTP surface. One run at a time; fields the goal cannot answer return to the caller."""

import atexit
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .agent import Agent
from .demo import load_environment

PAGE_TEXT_LIMIT = 4000
PAUSED = "awaiting_answer"
STOPPED = {"done", "blocked"}
HOST = "127.0.0.1"
PORT = 8767
TOKEN = ""
CALL_TIMEOUT = 120.0
RUN_TTL = 300.0
LOCK = threading.Lock()
RUN = None


def configure():
    """Read deployment settings from the environment, after any .env has been loaded."""
    global HOST, PORT, TOKEN, CALL_TIMEOUT, RUN_TTL
    HOST = os.environ.get("JEV_SERVICE_HOST", "127.0.0.1")
    PORT = int(os.environ.get("JEV_SERVICE_PORT", "8767"))
    TOKEN = os.environ.get("JEV_SERVICE_TOKEN", "")
    CALL_TIMEOUT = float(os.environ.get("JEV_CALL_TIMEOUT", "120"))
    RUN_TTL = float(os.environ.get("JEV_RUN_TTL", "300"))


class Busy(RuntimeError):
    """Another run holds the browser slot, or the addressed run no longer exists."""

    def __init__(self, run_id):
        super().__init__(f"Run {run_id} is active; answer or cancel it" if run_id else "No matching active run")
        self.run_id = run_id


def close():
    global RUN
    if RUN:
        RUN["agent"].close()
        RUN = None


def reap():
    if RUN and RUN["agent"].state["status"] == PAUSED and time.monotonic() - RUN["touched"] > RUN_TTL:
        close()


def envelope(run_id, snapshot, status=None):
    page = snapshot.get("page") or {}
    body = {
        "run_id": run_id,
        "status": status or snapshot["status"],
        "steps": len(snapshot["history"]),
        "elapsed_ms": snapshot["elapsed_ms"],
        "final_url": page.get("url"),
        "page_text": (page.get("text") or "")[:PAGE_TEXT_LIMIT],
    }
    question = snapshot.get("question")
    if question:
        # The caller wrote the goal, so it is omitted; the field and the page carry the actual question.
        body["question"] = {
            "field": question["field"],
            "page": {"title": question["page"]["title"], "text": question["page"]["text"][:PAGE_TEXT_LIMIT]},
            "recent_actions": question["recent_actions"],
        }
    return body


def drive():
    """Advance the active run until it stops, needs an answer, or exceeds the per-call budget."""
    run = RUN
    agent = run["agent"]
    deadline = time.monotonic() + CALL_TIMEOUT
    while agent.state["status"] not in STOPPED | {PAUSED}:
        if time.monotonic() > deadline:
            # The cap is a stop, not a pause: the form may be left partly filled.
            body = envelope(run["id"], agent.snapshot(), status="timeout")
            close()
            return body
        agent.command("tick")
    body = envelope(run["id"], agent.snapshot())
    if agent.state["status"] == PAUSED:
        run["touched"] = time.monotonic()
    else:
        close()
    return body


def start(body):
    global RUN
    url, goal = body.get("url"), body.get("goal")
    if not isinstance(url, str) or urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("Supply an http(s) url")
    if not isinstance(goal, str) or not 0 < len(goal.strip()) <= 2000:
        raise ValueError("Enter 1-2,000 characters")
    reap()
    if RUN:
        raise Busy(RUN["id"])
    RUN = {"id": "r_" + secrets.token_urlsafe(12), "agent": Agent(url, goal.strip()), "touched": time.monotonic()}
    return drive()


def answer(body):
    reap()
    if not RUN or RUN["id"] != body.get("run_id"):
        raise Busy(RUN["id"] if RUN else None)
    RUN["agent"].command("answer", {"text": body.get("text")})
    return drive()


def cancel(body):
    if not RUN or RUN["id"] != body.get("run_id"):
        raise Busy(RUN["id"] if RUN else None)
    run_id = RUN["id"]
    close()
    return {"run_id": run_id, "status": "cancelled"}


ROUTES = {"/run": start, "/answer": answer, "/cancel": cancel}


class Handler(BaseHTTPRequestHandler):
    server_version = "jev-service"

    def send(self, status, payload):
        content = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(content)

    def authorized(self):
        return bool(TOKEN) and secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {TOKEN}")

    def do_GET(self):
        if not self.authorized():
            return self.send(401, {"error": "Bearer token required"})
        if urlparse(self.path).path != "/health":
            return self.send(404, {"error": "Not found"})
        self.send(200, {"status": "ok", "run_id": RUN["id"] if RUN else None})

    def do_POST(self):
        if not self.authorized():
            return self.send(401, {"error": "Bearer token required"})
        route = ROUTES.get(urlparse(self.path).path)
        if not route:
            return self.send(404, {"error": "Not found"})
        if not LOCK.acquire(blocking=False):
            return self.send(409, {"error": "A browser step is already running"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length < 16384:
                raise ValueError("Invalid request size")
            self.send(200, route(json.loads(self.rfile.read(length))))
        except Busy as busy:
            self.send(409, {"error": str(busy), "run_id": busy.run_id})
        except (ValueError, TimeoutError) as error:
            # A paused run survives a rejected request; a run that failed mid-flight does not.
            if RUN and RUN["agent"].state["status"] != PAUSED:
                close()
            self.send(400, {"error": str(error)})
        except RuntimeError as error:
            close()
            self.send(502, {"error": str(error)})
        except Exception:
            close()
            self.send(500, {"error": "The run failed and was closed; no automatic retry."})
        finally:
            LOCK.release()

    def log_message(self, *_args):
        pass


def reaper():
    while True:
        time.sleep(5)
        if LOCK.acquire(blocking=False):
            try:
                reap()
            finally:
                LOCK.release()


def main():
    load_environment()
    configure()
    if not TOKEN:
        raise SystemExit("Set JEV_SERVICE_TOKEN before starting the service")
    atexit.register(close)
    threading.Thread(target=reaper, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Jev service: http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        close()


if __name__ == "__main__":
    main()
