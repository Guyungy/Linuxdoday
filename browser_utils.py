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


def start_browser(proxy=PROXY_DEFAULT, headless=False):
    """启动 playwright 持久化上下文（复用 browser_data 登录态）。

    返回 (context, page)。调用方用完必须 ctx.close()。
    headless=True 会被 Cloudflare 拦截（返回挑战页），所以默认 False。
    """
    kill_stale_chrome()
    p = sync_playwright().start()
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=USER_DATA_DIR,
        channel="chrome",          # 用系统已装的 Google Chrome（非 bundled chromium）
        headless=headless,
        proxy={"server": f"http://{proxy}"} if proxy else None,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1920, "height": 1080},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def check_login(page, timeout=45):
    """检查是否已登录 Linux.do：存在 #current-user 元素"""
    try:
        page.goto(BASE, timeout=timeout * 1000)
        page.wait_for_load_state("domcontentloaded")
        time.sleep(2)
        return page.locator("#current-user").count() > 0
    except Exception:
        return False


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
