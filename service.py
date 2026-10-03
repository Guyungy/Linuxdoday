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

Read endpoints (/status /topics /hot) carry no authentication by default, so the
default bind address is loopback (SERVICE_HOST=127.0.0.1). Set
PROTECT_READ_ENDPOINTS=true together with SERVICE_TOKEN before exposing the
service on a non-loopback address (containers do exactly that).
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

# 抓取子进程用这个前缀标记「降级但没失败」的现场（热榜/正文抓取失败等）。
# 见 linux_do_scraper.py 的 WARN_PREFIX，两处必须一致。
WARN_PREFIX = "WARN: "
# /status 里保留的 stderr 尾部长度：够定位，不至于把状态接口撑大。
MAX_STDERR_KEPT = 2000
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# 连续失败时每 N 轮向 stderr 重播一次：既不刷屏，也不至于让长期故障彻底没人看见。
FAILURE_REANNOUNCE_EVERY = 4

# 每轮子进程 stderr 的**完整**留档（POLL-78 追加交付物）。
# /status 里的 last_error / last_stderr 都是 tail() 出来的（4000 / 2000），长轮次
# 跑满时最早几个板块的行会被切掉 —— 判据 A1/A3 因此只能读成下界。这份文件不截断。
# （.gitignore 里有 *.log，落盘不会进版本库。）
STDERR_LOG_FILE = DATA_DIR / "scrape_stderr.log"

# 子进程 deadline 的墙钟轮询间隔：唤醒后最多滞后一个 tick 就判定超时。
DEADLINE_TICK_SECONDS = 5.0


def tail(text, limit=MAX_STDERR_KEPT):
    return (text or "").strip()[-limit:]


def atomic_write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text or "", encoding="utf-8")
    temporary.replace(path)


def kill_quietly(process):
    """kill 掉子进程；已经退出/还没起来都算无事发生。"""
    if process is None or process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        pass


def run_with_wall_clock_deadline(command, timeout, *, env=None, input_text=None,
                                 cwd=None, tick=DEADLINE_TICK_SECONDS):
    """按**墙钟** deadline 跑子进程：到点 kill，按超时收口。

    为什么不能用 subprocess.run(timeout=)：CPython 拿 time.monotonic 计时，而
    macOS 合盖休眠期间单调钟是**停走**的 —— 2026-09-29T20:11Z 那一轮墙钟跑了
    5h32m54s，进程内只累计了 123s，于是 SCRAPE_TIMEOUT_SECONDS=3600 永远够不到，
    服务一直挂在「抓取中」。

    这里主线程按墙钟（time.time，CLOCK_REALTIME，休眠期间照走）算 deadline，每次
    只把「剩余墙钟」交给等待：唤醒后第一眼就判定超时，不按剩余时间续命。子进程的
    阻塞式通信（含 stdin 写入）放到工作线程，主线程不阻塞在管道上。
    """
    if cwd is None:
        cwd = ROOT
    outcome = {}
    finished = threading.Event()

    def worker():
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            outcome["process"] = process
            stdout, stderr = process.communicate(input_text)
            outcome["result"] = subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr)
        except BaseException as exc:  # noqa: BLE001 —— 原样交回调用方判断（OSError 等）
            outcome["error"] = exc
        finally:
            finished.set()

    worker_thread = threading.Thread(target=worker, daemon=True)
    worker_thread.start()
    deadline = time.time() + timeout
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            kill_quietly(outcome.get("process"))
            worker_thread.join(5)
            kill_quietly(outcome.get("process"))  # 起得比 deadline 还慢的极端情况
            result = outcome.get("result")
            raise subprocess.TimeoutExpired(
                command, timeout,
                output=getattr(result, "stdout", None),
                stderr=getattr(result, "stderr", None),
            )
        if finished.wait(min(tick, remaining)):
            break
    worker_thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def extract_warnings(stderr):
    """从子进程 stderr 里捞出 WARN: 行 —— 成功轮次里唯一能带出降级现场的通道。"""
    found = []
    for line in (stderr or "").splitlines():
        marker = line.find(WARN_PREFIX)
        if marker == -1:
            continue
        message = line[marker + len(WARN_PREFIX):].strip()
        if message:
            found.append(message)
    return found


def resolve_host():
    """监听地址：默认回环。

    /status /topics /hot 默认不带鉴权，绑 0.0.0.0 等于同网段可读。
    容器里必须显式设 SERVICE_HOST=0.0.0.0（否则端口映射转发不进来），
    所以容器配方同时把 PROTECT_READ_ENDPOINTS 默认打开。
    """
    return os.environ.get("SERVICE_HOST", "127.0.0.1")


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
        # 去重签名：不能在 state["last_error"] 上做判断 —— run_once 每次开头都会把它
        # 清成 None（「正在跑」期间不该挂着上一轮的错），那会让去重永远失效。
        self._last_failure_message = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.state_lock = threading.Lock()
        # 本轮抓取的墙钟起点（调度按它顺延 next_run_at，见 scheduler）
        self.round_started = None
        self.state = {
            "status": "starting",
            "started_at": utc_now(),
            # 调度线程存活状态：P2 的故障形态是「服务自称健康、实际已停止抓取」，
            # /health 靠这两个字段才看得出来。
            "scheduler_alive": False,
            "last_tick_at": None,
            "last_started_at": None,
            "last_finished_at": None,
            "last_success_at": None,
            "last_error": None,
            "last_error_repeat": 0,
            "consecutive_failures": 0,
            "last_stderr": None,
            "last_stderr_bytes": 0,
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
            # 完整 stderr 的落点：last_error / last_stderr 都是尾部，头部只在这里。
            "stderr_log_file": os.path.relpath(STDERR_LOG_FILE, ROOT),
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

    def keep_full_stderr(self, stderr, stdout=""):
        """把子进程 stderr 整段落盘（stderr 为空时退回 stdout）。

        /status 只留尾部（last_error 4000 / last_stderr 2000）；长轮次跑满时被切掉的
        正是最早几个板块的行。这份文件一字不截，只补证据留存、不改抓取行为。
        """
        text = stderr or stdout or ""
        try:
            atomic_write_text(STDERR_LOG_FILE, text)
        except OSError as exc:
            # 落盘失败（磁盘满 / 只读挂载）不能连累抓取本身。
            print(f"[{utc_now()}] 完整 stderr 落盘失败: {exc!r}", file=sys.stderr, flush=True)
            return 0
        with self.state_lock:
            self.state["last_stderr_bytes"] = len(text)
        return len(text)

    def zero_rows_is_failure(self, count):
        """P1 服务层兜底：这一轮 0 条要不要判失败。

        只在「browser 模式 + 没指定 `--cats`」时成立 —— 那意味着默认板块全跑完了，
        十几个板块翻完不可能零新增。指定板块下的 0 条可能是正常结果；
        rss 模式另有 scraper 自己的 `assert_rows_present` 兜底。

        为什么要在 service 侧再判一次：`last_success_at` 是 **service** 刷的。
        只在 scraper 里改，等于把这个结论托付给子进程的自觉。
        """
        return self.mode == "browser" and not self.categories and count == 0

    def record_failure(self, message, stderr=None):
        """失败落账：连续失败计数 + 错误去重。

        - `consecutive_failures`：连续失败轮数，成功一轮即清零。
        - `last_error_repeat`：**同一条**错误连续出现的次数。重复的同一错误不再每轮
          重播 stderr（否则「每 6 小时一条 error」会变成新的背景噪音），但每
          FAILURE_REANNOUNCE_EVERY 轮仍重播一次，免得长期故障彻底没人看见。
        """
        with self.state_lock:
            same = self._last_failure_message == message
            self._last_failure_message = message
            repeats = self.state.get("last_error_repeat", 0)
            repeat = repeats + 1 if same else 1
            consecutive = self.state.get("consecutive_failures", 0) + 1
            self.state.update({
                "status": "error",
                "last_finished_at": utc_now(),
                "last_error": message,
                "last_error_repeat": repeat,
                "consecutive_failures": consecutive,
                "runs": self.state["runs"] + 1,
            })
            if stderr is not None:
                self.state["last_stderr"] = tail(stderr)
        if repeat == 1 or repeat % FAILURE_REANNOUNCE_EVERY == 0:
            suffix = f"，同一错误第 {repeat} 次" if repeat > 1 else ""
            print(f"[{utc_now()}] 抓取失败（连续 {consecutive} 轮{suffix}）: {message}",
                  file=sys.stderr, flush=True)
        return False, message

    def run_once(self, trigger="schedule"):
        if not self.lock.acquire(blocking=False):
            return False, "a scrape is already running"
        try:
            with self.state_lock:
                self.state.update({"status": "running", "last_started_at": utc_now(), "last_error": None})
            try:
                result = run_with_wall_clock_deadline(
                    self.command(), self.timeout, env=os.environ.copy(),
                )
            except subprocess.TimeoutExpired as exc:
                # 子进程已被 kill，把它死前吐出来的 stderr 整段留档再往上收口。
                # 这份文件只装**抓取轮次**的 stderr —— 放在这里就是为了不被后面
                # 推送/日报的超时顶掉（那两处的 stderr 短，/status 的尾部够用）。
                self.keep_full_stderr(
                    getattr(exc, "stderr", None), getattr(exc, "output", "") or "")
                raise
            # 成功/非零退出两条路都先整段留档，再决定怎么收口。
            self.keep_full_stderr(result.stderr)
            if result.returncode != 0:
                error = (result.stderr or result.stdout or "unknown scraper error")[-4000:]
                return self.record_failure(error, result.stderr)

            try:
                rows = json.loads(result.stdout or "[]")
                if not isinstance(rows, list):
                    raise ValueError("scraper output is not a JSON array")
            except (json.JSONDecodeError, ValueError) as exc:
                return self.record_failure(str(exc), result.stderr)

            if self.zero_rows_is_failure(len(rows)):
                # browser 模式跑完全部默认板块却 0 条：不写 latest_run.json，
                # 上一份好数据留着才看得出「停在哪」。
                return self.record_failure(
                    "browser 模式跑完全部默认板块却拿到 0 条：按失败处理，"
                    "不刷新 last_success_at（上一份好数据保留在 latest_run.json）",
                    result.stderr,
                )

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
                push = run_with_wall_clock_deadline(
                    [sys.executable, str(ROOT / "push_to_feishu.py"),
                     "--result-file", str(PUSH_RESULT_FILE)],
                    self.timeout,
                    input_text=json.dumps(to_push, ensure_ascii=False),
                    env=os.environ.copy(),
                )
                if push.returncode != 0:
                    error = (push.stderr or push.stdout or "unknown Feishu push error")[-4000:]
                    return self.record_failure(error, push.stderr)
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
            # 成功分支此前把 stderr 整段丢弃（只在 returncode!=0 时才用），
            # 于是抓取器采集到的降级现场（热榜/正文失败）一条也查不到。
            # 现在：WARN: 行进 last_error，原始 stderr 留在 last_stderr。
            warnings = extract_warnings(result.stderr)
            with self.state_lock:
                now = utc_now()
                self.state.update({
                    "status": "idle", "last_finished_at": now, "last_success_at": now,
                    "last_error": "；".join(warnings) if warnings else None,
                    "last_stderr": tail(result.stderr),
                    "last_new_topics": len(rows), "runs": self.state["runs"] + 1,
                    "last_pushed_topics": pushed,
                    # 成功一轮即清零：/status 里的连续失败数只表示「当前还在坏」。
                    "consecutive_failures": 0, "last_error_repeat": 0,
                })
                self._last_failure_message = None
            if self.should_report_today():
                self.run_daily_report()
            return True, f"scrape completed: {len(rows)} new topics"
        except subprocess.TimeoutExpired as exc:
            # 墙钟到点：子进程已被 kill，这里按超时收口（不再有「跨过 N×容差还活着」）。
            # 走 record_failure 以复用「连续失败计数 + 错误去重」（POLL-69）；
            # 同时把被 kill 子进程的 stderr 一并留档（POLL-78），那是最有价值的一份现场。
            error = f"scrape timed out after {self.timeout}s"
            print(f"[{utc_now()}] {error}（子进程已 kill）", file=sys.stderr, flush=True)
            timed_out_stderr = getattr(exc, "stderr", None)
            if timed_out_stderr:
                return self.record_failure(error, stderr=timed_out_stderr)
            return self.record_failure(error)
        except OSError as exc:
            # 部署产物缺文件（例如镜像里没有 COPY daily_report.py）时子进程根本起不来。
            # 这类异常以前直接冒到调度线程，把线程打死而服务仍自称健康。
            return self.record_failure(f"子进程无法启动（检查部署产物是否完整）: {exc!r}")
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

    def _record_report_error(self, message):
        """日报失败的现场写进 last_error（否则只在 stderr 里划过，没人看得到）。"""
        print(f"[{utc_now()}] {message}", file=sys.stderr, flush=True)
        with self.state_lock:
            self.state["last_error"] = message

    def run_daily_report(self):
        """生成日报；失败只记现场，不影响抓取主流程，更不允许打死调度线程。"""
        try:
            result = run_with_wall_clock_deadline(
                self.daily_report_command(), self.timeout, env=os.environ.copy())
            if result.returncode != 0:
                self._record_report_error(f"日报生成失败: {tail(result.stderr or result.stdout, 500)}")
                return False
            print(f"[{utc_now()}] 日报已生成 {result.stdout.strip()[-200:]}", file=sys.stderr, flush=True)
            return True
        except subprocess.TimeoutExpired:
            self._record_report_error(f"日报生成超时（{self.timeout}s）")
            return False
        except OSError as exc:
            # 典型场景：镜像里没有 COPY daily_report.py → FileNotFoundError。
            self._record_report_error(
                f"日报进程无法启动（确认 daily_report.py 在部署产物里）: {exc!r}")
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

    def safe_run_once(self, trigger):
        """调度线程的兜底：任何单轮异常都只记录，绝不让线程退出。

        此前 scheduler() 里两处 run_once 都没有 try/except，而 run_once 只捕获
        TimeoutExpired。于是 FileNotFoundError / OSError 会一路冒上来终结调度线程：
        HTTP 线程照常服务、/health 仍返回 ok=true —— 服务看起来活着，实际已停止抓取。
        """
        try:
            return self.run_once(trigger)
        except Exception as exc:  # noqa: BLE001 —— 兜底就是要宽
            message = f"{trigger} 轮次异常（调度线程继续运行）: {exc!r}"
            return self.record_failure(message)

    def announce_next_run(self):
        """next_run_at = **本轮起点** + interval，返回那个墙钟时刻。

        以前 due 在循环顶（上一轮收口之后）算，于是「一轮跑了 5h32m」会把下一轮整体
        推到 11h32m 之后 —— 轮次越慢，节奏越散。现在锚定轮次起点：一轮跑了多久都只
        从起点顺延。起点已经过去整个 interval（上一轮跑满）时立即接一轮，不空转：
        下一轮跑完会重新锚定。
        """
        anchor = self.round_started or datetime.now(timezone.utc)
        due = anchor + timedelta(seconds=self.interval)
        now = datetime.now(timezone.utc)
        if due <= now:
            due = now
        with self.state_lock:
            self.state["next_run_at"] = due.isoformat(timespec="seconds")
        return due

    def run_round(self, trigger):
        """跑一轮：起点先定下 next_run_at，整轮期间 /status 里的值都是准的。"""
        self.round_started = datetime.now(timezone.utc)
        self.announce_next_run()
        return self.safe_run_once(trigger)

    def scheduler(self):
        """调度线程：任何一轮都不许把它打死，且存活状态必须能被 /health 看到。

        `scheduler_alive` / `last_tick_at` 是 AC2.3 的判据 —— 线程退出（正常停止或
        异常逃逸）后 /health 立刻不再 ok，故障不再表现为「全绿」。

        计时与轮次编排走 run_round / announce_next_run（POLL-78 墙钟口径）：
        next_run_at 在**每轮起点**就定下来，整轮期间读到的值都是准的。
        """
        with self.state_lock:
            self.state["scheduler_alive"] = True
            self.state["last_tick_at"] = utc_now()
        try:
            if self.should_run_on_start():
                self.run_round("startup")
            else:
                self.round_started = datetime.now(timezone.utc)
                with self.state_lock:
                    self.state["status"] = "idle"
            while True:
                if not self.wait_until(self.announce_next_run()):
                    break
                with self.state_lock:
                    self.state["last_tick_at"] = utc_now()
                self.run_round("schedule")
        finally:
            # 正常停止与异常逃逸都要落到这里：否则线程死了、/health 还全绿。
            with self.state_lock:
                self.state["scheduler_alive"] = False
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
            state = SERVICE.snapshot()
            alive = bool(state["scheduler_alive"])
            # 仍然返回 200：/health 是只读探针，形状不变（容器探针/脚本靠它）。
            # 但调度线程不在时 `ok` 必须为 false —— 否则「服务活着、其实已停止抓取」
            # 这个 P2 故障形态就还是全绿。
            self.send_json(200, {
                "ok": alive,
                "status": state["status"],
                "scheduler_alive": alive,
                "last_tick_at": state["last_tick_at"],
                "consecutive_failures": state["consecutive_failures"],
            })
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


def startup_warnings():
    """启动时的部署姿态警告（非静默）：main() 打印，测试直接断言。

    1. 读接口开了鉴权却没有令牌 → /status /topics /hot 一律 401，
       而「读鉴权开着」这个信号本身就在 /status 里，于是连它都看不到。
    2. 对外监听却关掉了读鉴权 → 端口能连上的人都读得到。
    """
    messages = []
    if SERVICE.protect_reads and not SERVICE.token:
        messages.append("⚠️ PROTECT_READ_ENDPOINTS=true 但 SERVICE_TOKEN 为空："
                        "/status /topics /hot 将一律返回 401。")
    elif not SERVICE.protect_reads and resolve_host() not in LOOPBACK_HOSTS:
        host = resolve_host()
        messages.append(f"⚠️ SERVICE_HOST={host} 对外监听且 PROTECT_READ_ENDPOINTS=false："
                        "/status /topics /hot 无鉴权可读。建议设 PROTECT_READ_ENDPOINTS=true"
                        "（并配置 SERVICE_TOKEN），或改回 127.0.0.1。")
    return messages


def main():
    parser = argparse.ArgumentParser(description="Linuxdoday background service")
    parser.add_argument("--once", action="store_true", help="run one scrape and exit")
    args = parser.parse_args()
    if args.once:
        ok, message = SERVICE.run_once("cli")
        print(message)
        raise SystemExit(0 if ok else 1)

    # 默认只监听回环：/status /topics /hot 默认无鉴权，绑 0.0.0.0 等于同网段可读。
    # 容器里要发布端口时才显式设 SERVICE_HOST=0.0.0.0（见 docker-compose.service.yml）。
    host = resolve_host()
    port = int(os.environ.get("SERVICE_PORT", "8080"))
    for message in startup_warnings():
        print(message, file=sys.stderr, flush=True)
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
