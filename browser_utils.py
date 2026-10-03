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
            ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5,
        ).stdout
        pids = []
        for line in out.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            pid, command = parts
            if ("Google Chrome.app/Contents/MacOS/Google Chrome" in command
                    and f"--user-data-dir={USER_DATA_DIR}" in command):
                pids.append(int(pid))
        if pids:
            for pid in pids:
                try:
                    os.kill(pid, 15)
                except ProcessLookupError:
                    pass
            log(f"已清理项目 Chrome 进程: {', '.join(map(str, pids))}")
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


_JS_SESSION = """
async () => {
    try {
        const r = await fetch('/session/current.json',
            {credentials: 'include', headers: {Accept: 'application/json'}});
        if (!r.ok) return {status: r.status};
        const j = await r.json();
        return {status: 200, user: ((j.current_user || {}).username) || null};
    } catch (e) { return {status: 'ERR ' + e}; }
}
"""


def check_session(page, retries=3, interval=4, goto=True):
    """用 /session/current.json 判登录态，返回 (ok, username)。

    比 check_login 稳：不依赖 `#current-user` 是否渲染出来 —— CF 挑战页上 DOM 里
    没有该元素，会给出「未登录」的假警报（实测同一会话一会儿 True 一会儿 False），
    而 JSON 接口在 cf_clearance 有效时直接给 200 + current_user。
    """
    if goto and "linux.do" not in (page.url or ""):
        try:
            page.goto(BASE, timeout=45000, wait_until="domcontentloaded")
            time.sleep(2)
        except Exception:
            pass
    for i in range(max(1, retries)):
        try:
            r = page.evaluate(_JS_SESSION)
        except Exception as exc:
            r = {"status": f"EXC {type(exc).__name__}"}
        if isinstance(r, dict) and r.get("status") == 200 and r.get("user"):
            return True, r["user"]
        if i < retries - 1:
            time.sleep(interval)
    return False, None


def wait_cf_challenge(page, url, timeout=60):
    """等待 Cloudflare 挑战完成（title 不再含 'Just a moment'）"""
    try:
        page.goto(url, timeout=timeout * 1000)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            title = page.title() or ""
        except Exception:
            title = ""
        if "Just a moment" not in title and "Attention Required" not in title:
            return True
        time.sleep(2)
    log(f"⚠️ Cloudflare 挑战超时（{timeout}s），当前 title: {page.title()}")
    return False
