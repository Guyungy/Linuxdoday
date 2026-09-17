#!/usr/bin/env python3
"""Linuxdoday cross-platform background service.

Runs the browser-free RSS scraper on a schedule and exposes a small HTTP API.
Only Python's standard library is used by the service itself.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LATEST_FILE = DATA_DIR / "latest_run.json"
PENDING_FEISHU_FILE = DATA_DIR / "pending_feishu.json"


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ScrapeService:
    def __init__(self):
        self.interval = max(60, int(os.environ.get("SCRAPE_INTERVAL_SECONDS", "21600")))
        self.timeout = max(60, int(os.environ.get("SCRAPE_TIMEOUT_SECONDS", "1800")))
        self.run_on_start = env_bool("RUN_ON_START", True)
        self.categories = os.environ.get("SCRAPE_CATEGORIES", "").strip()
        self.limit = max(0, int(os.environ.get("SCRAPE_LIMIT", "0")))
        self.total_limit = max(0, int(os.environ.get("SCRAPE_TOTAL_LIMIT", "0")))
        self.rss_pages = max(1, int(os.environ.get("SCRAPE_RSS_PAGES", "1")))
        self.with_content = env_bool("SCRAPE_CONTENT", False)
        self.push_to_feishu = env_bool("PUSH_TO_FEISHU", False)
        self.token = os.environ.get("SERVICE_TOKEN", "").strip()
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.state_lock = threading.Lock()
        self.state = {
            "status": "starting",
            "started_at": utc_now(),
            "last_started_at": None,
            "last_finished_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_new_topics": 0,
            "last_pushed_topics": 0,
            "runs": 0,
        }

    def snapshot(self):
        with self.state_lock:
            result = dict(self.state)
        result.update({
            "interval_seconds": self.interval,
            "categories": self.categories.split(",") if self.categories else "enabled_defaults",
            "content_enabled": self.with_content,
            "feishu_push_enabled": self.push_to_feishu,
            "manual_run_enabled": bool(self.token),
        })
        return result

    def command(self):
        command = [sys.executable, str(ROOT / "linux_do_scraper.py"), "--scrape", "--rss"]
        if not os.environ.get("LINUXDO_PROXY"):
            command.append("--no-proxy")
        if self.categories:
            command.extend(["--cats", self.categories])
        if self.limit:
            command.extend(["--limit", str(self.limit)])
        if self.total_limit:
            command.extend(["--total-limit", str(self.total_limit)])
        if self.rss_pages > 1:
            command.extend(["--rss-pages", str(self.rss_pages)])
        if self.with_content:
            command.append("--content")
        return command

    def run_once(self, trigger="schedule"):
        if not self.lock.acquire(blocking=False):
            return False, "a scrape is already running"
        try:
            with self.state_lock:
                self.state.update({"status": "running", "last_started_at": utc_now(), "last_error": None})
            result = subprocess.run(
                self.command(), cwd=ROOT, capture_output=True, text=True, timeout=self.timeout,
                env=os.environ.copy(),
            )
            if result.returncode != 0:
                error = (result.stderr or result.stdout or "unknown scraper error")[-4000:]
                with self.state_lock:
                    self.state.update({
                        "status": "error", "last_finished_at": utc_now(), "last_error": error,
                        "runs": self.state["runs"] + 1,
                    })
                return False, error

            try:
                rows = json.loads(result.stdout or "[]")
                if not isinstance(rows, list):
                    raise ValueError("scraper output is not a JSON array")
            except (json.JSONDecodeError, ValueError) as exc:
                with self.state_lock:
                    self.state.update({
                        "status": "error", "last_finished_at": utc_now(), "last_error": str(exc),
                        "runs": self.state["runs"] + 1,
                    })
                return False, str(exc)

            pushed = 0
            if self.push_to_feishu:
                try:
                    pending = json.loads(PENDING_FEISHU_FILE.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    pending = []
                by_id = {str(row.get("Topic ID")): row for row in pending if row.get("Topic ID")}
                by_id.update({str(row.get("Topic ID")): row for row in rows if row.get("Topic ID")})
                to_push = list(by_id.values())
            else:
                to_push = []

            if to_push:
                DATA_DIR.mkdir(parents=True, exist_ok=True)
                PENDING_FEISHU_FILE.write_text(
                    json.dumps(to_push, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                push = subprocess.run(
                    [sys.executable, str(ROOT / "push_to_feishu.py")],
                    cwd=ROOT,
                    input=json.dumps(to_push, ensure_ascii=False),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    env=os.environ.copy(),
                )
                if push.returncode != 0:
                    error = (push.stderr or push.stdout or "unknown Feishu push error")[-4000:]
                    with self.state_lock:
                        self.state.update({
                            "status": "error", "last_finished_at": utc_now(), "last_error": error,
                            "runs": self.state["runs"] + 1,
                        })
                    return False, error
                pushed = len(to_push)
                PENDING_FEISHU_FILE.unlink(missing_ok=True)

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"generated_at": utc_now(), "trigger": trigger, "count": len(rows), "topics": rows}
            temporary = LATEST_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(LATEST_FILE)
            with self.state_lock:
                now = utc_now()
                self.state.update({
                    "status": "idle", "last_finished_at": now, "last_success_at": now,
                    "last_error": None, "last_new_topics": len(rows), "runs": self.state["runs"] + 1,
                    "last_pushed_topics": pushed,
                })
            return True, f"scrape completed: {len(rows)} new topics"
        except subprocess.TimeoutExpired:
            error = f"scrape timed out after {self.timeout}s"
            with self.state_lock:
                self.state.update({
                    "status": "error", "last_finished_at": utc_now(), "last_error": error,
                    "runs": self.state["runs"] + 1,
                })
            return False, error
        finally:
            self.lock.release()

    def scheduler(self):
        if self.run_on_start:
            self.run_once("startup")
        else:
            with self.state_lock:
                self.state["status"] = "idle"
        while not self.stop_event.wait(self.interval):
            self.run_once("schedule")

    def topics(self):
        try:
            return json.loads(LATEST_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"generated_at": None, "count": 0, "topics": []}


SERVICE = ScrapeService()


class Handler(BaseHTTPRequestHandler):
    server_version = "Linuxdoday/1.0"

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            self.send_json(200, {"ok": True, "status": SERVICE.snapshot()["status"]})
        elif path == "/status":
            self.send_json(200, SERVICE.snapshot())
        elif path == "/topics":
            self.send_json(200, SERVICE.topics())
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != "/run":
            self.send_json(404, {"error": "not found"})
            return
        if not SERVICE.token:
            self.send_json(403, {"error": "manual runs are disabled; configure SERVICE_TOKEN"})
            return
        if self.headers.get("Authorization") != f"Bearer {SERVICE.token}":
            self.send_json(401, {"error": "unauthorized"})
            return
        if SERVICE.lock.locked():
            self.send_json(409, {"error": "a scrape is already running"})
            return
        threading.Thread(target=SERVICE.run_once, args=("api",), daemon=True).start()
        self.send_json(202, {"accepted": True})

    def log_message(self, fmt, *args):
        print(f"[{utc_now()}] {self.address_string()} {fmt % args}", file=sys.stderr, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Linuxdoday background service")
    parser.add_argument("--once", action="store_true", help="run one scrape and exit")
    args = parser.parse_args()
    if args.once:
        ok, message = SERVICE.run_once("cli")
        print(message)
        raise SystemExit(0 if ok else 1)

    host = os.environ.get("SERVICE_HOST", "0.0.0.0")
    port = int(os.environ.get("SERVICE_PORT", "8080"))
    thread = threading.Thread(target=SERVICE.scheduler, daemon=True)
    thread.start()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Linuxdoday service listening on http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SERVICE.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
