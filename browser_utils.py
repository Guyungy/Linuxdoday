#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
browser_utils.py — Linux.do 抓取的 playwright 真实浏览器公共模块

背景：DrissionPage 4.x 与 Chrome 153 不兼容（WebSocket 404 / PageDisconnectedError），
macOS 上浏览器路径自动探测还会返回字面 "chrome" 导致启动失败。
改用 playwright launch_persistent_context 复用 browser_data/ 登录态，
headless=False 下可正常通过 Cloudflare Turnstile 托管挑战。

用法（供 fetch_content.py / linux_do_scraper.py 共用）：
    from browser_utils import start_browser, check_login
    ctx, page = start_browser(proxy="127.0.0.1:7897", headless=False)
    ...
    ctx.close()
"""

import os
import subprocess
import sys
import time
from datetime import datetime
from typing import NamedTuple

from playwright.sync_api import sync_playwright

BASE = "https://linux.do"
PROXY_DEFAULT = os.environ.get("LINUXDO_PROXY", "")
USER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_data")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def kill_stale_chrome():
    """清理残留 Chrome：以 browser_data 为 user-data-dir 的实例会锁住 profile，
    导致 launch_persistent_context 无法复用登录态。只杀本项目自己的实例。"""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "browser_data"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        if out:
            for pid in out.splitlines():
                try:
                    os.kill(int(pid), 15)
                except Exception:
                    pass
            log(f"已清理残留 Chrome 进程: {out.replace(chr(10), ', ')}")
            time.sleep(2)
    except Exception:
        pass


def start_browser(proxy=PROXY_DEFAULT, headless=False, offscreen=True):
    """启动 playwright 持久化上下文（复用 browser_data 登录态）。

    返回 (context, page)。调用方用完必须 ctx.close()。
    headless=True 会被 Cloudflare 拦截（返回挑战页），所以默认 False。
    offscreen=True（默认）：窗口开到屏幕外，不抢焦点、不遮挡桌面，但仍是
      有头真实浏览器 —— 实测可正常通过 Cloudflare（headless 则 403）。
    """
    kill_stale_chrome()
    p = sync_playwright().start()
    args = ["--disable-blink-features=AutomationControlled"]
    if offscreen and not headless:
        # 移到可视区域之外；窗口仍存在，只是用户看不见
        args += ["--window-position=-2400,-2400", "--window-size=1280,900"]
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR,
        channel="chrome",          # 用系统已装的 Google Chrome（非 bundled chromium）
        headless=headless,
        proxy={"server": f"http://{proxy}"} if proxy else None,
        args=args,
        viewport={"width": 1920, "height": 1080},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def wait_json_ready(page, slug="develop/4", timeout=90, interval=3):
    """等待 Cloudflare cookie 就绪：浏览器内 fetch 板块 JSON 直到返回 200。

    刚 launch_persistent_context + goto 后立刻 fetch 常拿到 403（cf_clearance
    还没完全生效），等几秒后即正常。返回 True/False。
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = page.evaluate("""async (slug) => {
                try {
                    const r = await fetch("/c/" + slug + ".json?page=0",
                        {credentials: "include", headers: {"Accept": "application/json"}});
                    return r.status;
                } catch (e) { return "ERR:" + e; }
            }""", slug)
        except Exception as exc:
            last = f"EXC:{type(exc).__name__}"
        if last == 200:
            return True
        time.sleep(interval)
    log(f"⚠️ JSON 接口就绪等待超时（{timeout}s），最后状态: {last}")
    return False


def check_login(page, timeout=45):
    """检查是否已登录 Linux.do：存在 #current-user 元素"""
    try:
        page.goto(BASE, timeout=timeout * 1000)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)
        return page.locator("#current-user").count() > 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Cloudflare 挑战判据
#
# 判据必须**说得清**：旧版只有「title 里没有 'Just a moment' / 'Attention Required'」
# 一条，于是下面四种情况一律返回 True（判定挑战已过），随后 DOM 查出 0 行却不报错：
#   1) page.goto 抛异常，页面其实停在旧页（异常被 except 吞掉）；
#   2) title 读不到（空串）；
#   3) 中文挑战页（'请稍候…'）；
#   4) 确实到了别的页面（CF 拦截页 / 错误页）。
# 现在改成「必须拿到正向证据才算通过」，并且把「未确认」如实报出来。
# ---------------------------------------------------------------------------

CF_PASSED = "passed"            # 已确认：挑战已过，且页面确实是目标站点页面
CF_NAV_FAILED = "nav_failed"    # 导航本身失败（goto 抛异常），目标页连样子都没见到
CF_CHALLENGE = "challenge"      # 确认仍在挑战页（中英 title 特征或挑战页 DOM 标记）
CF_UNCONFIRMED = "unconfirmed"  # 到点也没能确认：title 读不到 / 停在非目标页面

# title 特征（中英双语）。原判据只认前两个英文串 —— 中文挑战页（CF 会把
# 「Just a moment…」本地化成「请稍候…」）因此被判成「已通过」。
CF_CHALLENGE_TITLE_HINTS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "verify you are human",
    "请稍候",
    "正在检查您的浏览器",
    "人机验证",
)

# 挑战页 DOM 标记：加载中的挑战页 title 可能是空的，只靠 title 看不出。
CF_CHALLENGE_DOM_SELECTOR = ", ".join((
    "#challenge-running",
    "#challenge-stage",
    "#challenge-form",
    "#cf-challenge-running",
    "#cf-please-wait",
    "#turnstile-wrapper",
    "div.cf-turnstile",
    'iframe[src*="challenges.cloudflare.com"]',
))

# 目标站点的正向标记（Discourse 标准布局 / generator meta）。
# 「真的到了 Linux.do」只能由正向标记确认 —— 没有它就只能算未确认。
PAGE_READY_DOM_SELECTOR = ", ".join((
    "#main-outlet",
    ".topic-list",
    "#d-header",
    ".d-header",
    'meta[name="generator"][content*="Discourse"]',
))

# 一次探测同时取回 title / 挑战标记 / 目标页标记：省往返，且三者取自同一时刻。
_PROBE_JS = """
(args) => {
    const {challengeSel, readySel} = args;
    const has = (sel) => { try { return !!document.querySelector(sel); } catch (e) { return false; } };
    return {
        title: document.title || "",
        url: location.href,
        challenge: has(challengeSel),
        ready: has(readySel),
    };
}
"""


class CfChallengeOutcome(NamedTuple):
    """wait_cf_challenge 的结论。

    **bool(结论) 只在「已确认通过」时为 True** —— 任何「未确认」状态都是假值，
    于是漏改的 `if not wait_cf_challenge(...)` 仍然失败在安全的一侧（不会被当成成功）。
    调用方要区分细节时读 status / detail，不要再把它压回一个 bool。
    """

    status: str
    detail: str = ""
    title: str = ""
    url: str = ""

    @property
    def passed(self):
        return self.status == CF_PASSED

    def describe(self):
        """给 last_error / 日志用的一句话现场。"""
        parts = [self.status]
        if self.detail:
            parts.append(self.detail)
        if self.title:
            parts.append(f"title={self.title!r}")
        if self.url:
            parts.append(f"url={self.url}")
        return " | ".join(parts)

    def __bool__(self):
        return self.status == CF_PASSED


def _title_is_challenge(title):
    low = (title or "").lower()
    return any(hint in low for hint in CF_CHALLENGE_TITLE_HINTS)


def _probe_page(page):
    """探测当前页面 → (probe, error)。probe 为 None 表示这次探测不可用
    （页面正在导航 / 执行上下文被销毁）——**不可用 ≠ 通过**，继续等下一轮。"""
    try:
        probe = page.evaluate(_PROBE_JS, {
            "challengeSel": CF_CHALLENGE_DOM_SELECTOR,
            "readySel": PAGE_READY_DOM_SELECTOR,
        })
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(probe, dict):
        return None, f"探测返回非预期结构（{type(probe).__name__}）"
    return probe, None


def wait_cf_challenge(page, url, timeout=60, interval=2):
    """等待 Cloudflare 挑战完成，并**如实报告结论**（返回 CfChallengeOutcome）。

    判定 passed 必须同时满足三条：
      1) page.goto 没有抛异常（导航真的发生了）；
      2) title 非空，且不含中英挑战页特征；
      3) 页面上出现了目标站点的正向标记（Discourse 的 #main-outlet 等）。
    只有 (2) 而没有 (3) 不算通过 —— 「title 里没有 Just a moment」正是那条缝。

    导航失败时**立即**返回 CF_NAV_FAILED，不再拿旧页的 title 去猜：goto 抛异常
    说明目标页没打开，此时任何「看起来正常」的 title 都来自上一个页面。
    """
    try:
        page.goto(url, timeout=timeout * 1000)
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        log(f"⚠️ 导航失败（{detail}）: {url}")
        return CfChallengeOutcome(CF_NAV_FAILED, detail, "", url)

    deadline = time.time() + timeout
    last = CfChallengeOutcome(CF_UNCONFIRMED, "探测未执行", "", url)

    while True:
        probe, error = _probe_page(page)
        if probe is None:
            last = CfChallengeOutcome(CF_UNCONFIRMED, f"页面探测失败: {error}", last.title, url)
        else:
            title = probe.get("title") or ""
            landed = probe.get("url") or url
            if probe.get("challenge") or _title_is_challenge(title):
                last = CfChallengeOutcome(CF_CHALLENGE, "仍在 Cloudflare 挑战页", title, landed)
            elif not title.strip():
                last = CfChallengeOutcome(CF_UNCONFIRMED, "页面 title 为空，无法确认已离开挑战页", title, landed)
            elif probe.get("ready"):
                return CfChallengeOutcome(CF_PASSED, "已确认通过", title, landed)
            else:
                last = CfChallengeOutcome(CF_UNCONFIRMED, "已离开挑战页，但不是目标站点页面", title, landed)

        if time.time() >= deadline:
            break
        time.sleep(interval)

    log(f"⚠️ Cloudflare 判据未确认通过（{timeout}s）: {last.describe()}")
    return last
