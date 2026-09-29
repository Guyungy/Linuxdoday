#!/usr/bin/env python3
"""Linuxdoday cross-platform background service.

Runs the scraper on a schedule and exposes a small HTTP API.

Two scrape modes (SCRAPE_MODE):
  - rss     (default) browser-free HTTP RSS: fast, but no views/replies.
  - browser playwright offscreen Chrome: full metrics (views/replies), needs a
    GUI session and the playwright extra dependencies.

Only Python's standard library is used by the service itself; the scraper
subprocess is launched with the same interpreter, so that interpreter must have
the dependencies of the selected mode installed.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
LATEST_FILE = DATA_DIR / "latest_run.json"
PENDING_FEISHU_FILE = DATA_DIR / "pending_feishu.json"
PUSH_RESULT_FILE = DATA_DIR / ".push_result.json"
HOT_FILE = DATA_DIR / "hot_topics.json"


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


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
        mode = os.environ.get("SCRAPE_MODE", "rss").strip().lower()
        self.mode = mode if mode in {"rss", "browser"} else "rss"
        self.max_pages = max(0, int(os.environ.get("SCRAPE_MAX_PAGES", "0")))
        self.with_content = env_bool("SCRAPE_CONTENT", False)
        self.content_limit = max(0, int(os.environ.get("SCRAPE_CONTENT_LIMIT", "0")))
        # 官方热榜一次请求即可拿到，browser 模式默认开启
        self.hot_enabled = env_bool("SCRAPE_HOT", self.mode == "browser")
        self.push_to_feishu = env_bool("PUSH_TO_FEISHU", False)
        # AI 日报：每天只生成一次（靠 reports/ 里当天文件是否存在去重）
        self.daily_report = env_bool("DAILY_REPORT", False)
        self.report_chat = os.environ.get("DAILY_REPORT_CHAT", "").strip()
        self.report_push_as = (os.environ.get("DAILY_REPORT_PUSH_AS", "auto").strip() or "auto")
        self.report_card = env_bool("DAILY_REPORT_CARD", True)
        self.report_model = os.environ.get("DAILY_REPORT_MODEL", "").strip()
        self.report_hour = max(0, min(23, int(os.environ.get("DAILY_REPORT_HOUR", "8"))))
        self.token = os.environ.get("SERVICE_TOKEN", "").strip()
        self.protect_reads = env_bool("PROTECT_READ_ENDPOINTS", False)
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
            "next_run_at": None,
            "runs": 0,
        }
        self._seed_state_from_history()

    def _seed_state_from_history(self):
        """重启后 /status 不应显示"从未成功过"：用落盘的上次结果回填。"""
        try:
            payload = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        generated = payload.get("generated_at")
        if generated:
            self.state["last_success_at"] = generated
            self.state["last_finished_at"] = generated
        try:
            self.state["last_new_topics"] = int(payload.get("count") or 0)
        except (TypeError, ValueError):
            pass

    def snapshot(self):
        with self.state_lock:
            result = dict(self.state)
        result.update({
            "interval_seconds": self.interval,
            "categories": self.categories.split(",") if self.categories else "enabled_defaults",
            "mode": self.mode,
            "max_pages": self.max_pages or (3 if self.mode == "browser" else None),
            "content_enabled": self.with_content,
            "content_limit": self.content_limit,
            "hot_enabled": self.hot_enabled,
            "daily_report_enabled": self.daily_report,
            "daily_report_hour": self.report_hour,
            "daily_report_chat": self.report_chat or None,
            "feishu_push_enabled": self.push_to_feishu,
            "manual_run_enabled": bool(self.token),
            "read_auth_enabled": self.protect_reads,
        })
        return result

    def command(self):
        command = [sys.executable, str(ROOT / "linux_do_scraper.py"), "--scrape"]
        if self.mode != "browser":
            command.append("--rss")
        if not os.environ.get("LINUXDO_PROXY"):
            command.append("--no-proxy")
        if self.categories:
            command.extend(["--cats", self.categories])
        if self.limit:
            command.extend(["--limit", str(self.limit)])
        if self.total_limit:
            command.extend(["--total-limit", str(self.total_limit)])
        if self.mode != "browser" and self.rss_pages > 1:
            command.extend(["--rss-pages", str(self.rss_pages)])
        if self.mode == "browser" and self.max_pages:
            command.extend(["--max-pages", str(self.max_pages)])
        if self.mode == "browser" and self.hot_enabled:
            command.append("--hot")
        if self.with_content:
            command.append("--content")
            if self.content_limit:
                command.extend(["--content-limit", str(self.content_limit)])
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
                atomic_write_json(PENDING_FEISHU_FILE, to_push)
                PUSH_RESULT_FILE.unlink(missing_ok=True)
                push = subprocess.run(
                    [sys.executable, str(ROOT / "push_to_feishu.py"),
                     "--result-file", str(PUSH_RESULT_FILE)],
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
                try:
                    push_result = json.loads(PUSH_RESULT_FILE.read_text(encoding="utf-8"))
                    pushed = int(push_result.get("written", 0))
                except (OSError, ValueError, json.JSONDecodeError):
                    pushed = 0
                PENDING_FEISHU_FILE.unlink(missing_ok=True)
                PUSH_RESULT_FILE.unlink(missing_ok=True)

            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"generated_at": utc_now(), "trigger": trigger, "count": len(rows), "topics": rows}
            atomic_write_json(LATEST_FILE, payload)
            with self.state_lock:
                now = utc_now()
                self.state.update({
                    "status": "idle", "last_finished_at": now, "last_success_at": now,
                    "last_error": None, "last_new_topics": len(rows), "runs": self.state["runs"] + 1,
                    "last_pushed_topics": pushed,
                })
            if self.should_report_today():
                self.run_daily_report()
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

    def should_report_today(self):
        """每天只出一份日报：过了 report_hour 且当天文件还不存在。"""
        if not self.daily_report:
            return False
        now = datetime.now()
        if now.hour < self.report_hour:
            return False
        return not (REPORT_DIR / f"AI日报-{now.date()}.md").exists()

    def daily_report_command(self):
        command = [sys.executable, str(ROOT / "daily_report.py")]
        if self.report_model:
            command.extend(["--model", self.report_model])
        if self.report_chat:
            command.extend(["--push-chat", self.report_chat])
        if self.report_push_as != "auto":
            command.extend(["--push-as", self.report_push_as])
        if self.report_card:
            command.append("--card")
        return command

    def run_daily_report(self):
        """生成日报；失败只记日志，不影响抓取主流程。"""
        try:
            result = subprocess.run(self.daily_report_command(), cwd=ROOT,
                                    capture_output=True, text=True, timeout=self.timeout,
                                    env=os.environ.copy())
            if result.returncode != 0:
                print(f"[{utc_now()}] 日报生成失败: {(result.stderr or result.stdout)[-500:]}",
                      file=sys.stderr, flush=True)
                return False
            print(f"[{utc_now()}] 日报已生成 {result.stdout.strip()[-200:]}", file=sys.stderr, flush=True)
            return True
        except subprocess.TimeoutExpired:
            print(f"[{utc_now()}] 日报生成超时", file=sys.stderr, flush=True)
            return False

    def wait_until(self, due, tick=30):
        """分片等待到指定墙钟时间。

        macOS 合盖休眠会挂起进程，单次长 wait 的剩余时间会被整体拉长
        （6 小时变成"睡多久就晚多久"）。改为短睡眠 + 按墙钟重算剩余时间，
        唤醒后最多滞后一个 tick 就立即执行。返回 True=到点，False=被要求停止。
        """
        while True:
            remaining = (due - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                return True
            if self.stop_event.wait(min(tick, remaining)):
                return False

    def should_run_on_start(self):
        """启动时是否立即抓取。

        最近一轮成功距今不足 interval/2 就跳过：维护/重启很频繁，
        每次重启都打一轮全量抓取既慢又容易撞限流。没有历史则照跑。
        """
        if not self.run_on_start:
            return False
        try:
            payload = json.loads(LATEST_FILE.read_text(encoding="utf-8"))
            generated = datetime.fromisoformat(str(payload.get("generated_at", "")).replace("Z", "+00:00"))
        except (OSError, ValueError, TypeError):
            return True
        if generated.tzinfo is None:
            generated = generated.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - generated).total_seconds()
        return age >= self.interval / 2

    def scheduler(self):
        if self.should_run_on_start():
            self.run_once("startup")
        else:
            with self.state_lock:
                self.state["status"] = "idle"
        while True:
            due = datetime.now(timezone.utc) + timedelta(seconds=self.interval)
            with self.state_lock:
                self.state["next_run_at"] = due.isoformat(timespec="seconds")
            if not self.wait_until(due):
                break
            self.run_once("schedule")
        with self.state_lock:
            self.state["next_run_at"] = None

    def topics(self):
        try:
            return json.loads(LATEST_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"generated_at": None, "count": 0, "topics": []}

    def hot(self):
        """官方热榜（data/hot_topics.json），由抓取轮次中的 --hot 写入。"""
        try:
            return json.loads(HOT_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"generated_at": None, "daily": [], "weekly": []}


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
        elif path == "/ready":
            state = SERVICE.snapshot()
            ready = state["status"] != "starting"
            self.send_json(200 if ready else 503, {"ready": ready, "status": state["status"]})
        elif path == "/status":
            if not self.read_authorized():
                return
            self.send_json(200, SERVICE.snapshot())
        elif path == "/topics":
            if not self.read_authorized():
                return
            self.send_json(200, SERVICE.topics())
        elif path == "/hot":
            if not self.read_authorized():
                return
            self.send_json(200, SERVICE.hot())
        else:
            self.send_json(404, {"error": "not found"})

    def read_authorized(self):
        if not SERVICE.protect_reads:
            return True
        if SERVICE.token and self.headers.get("Authorization") == f"Bearer {SERVICE.token}":
            return True
        self.send_json(401, {"error": "unauthorized"})
        return False

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
