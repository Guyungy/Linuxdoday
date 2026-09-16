#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_content.py — 抓取 Linux.do 帖子正文（全量回填/增量补全）

为什么独立成脚本：
  - 正文抓取是"每条一请求"，与列表抓取节奏完全不同，放主 scraper 会拖慢每日增量
  - 支持全量回填历史（--all），也支持只补没有正文的（默认）
  - 正文存本地 data/topic_content.json（{topic_id: {content, excerpt, fetched_at}}），
    不直接写入多维表格（正文太长会撑爆表），需要时可用 fetch_content 的 --feishu 导出短摘要

用法：
  python fetch_content.py                 # 只补缓存里没有正文的帖子（增量）
  python fetch_content.py --all           # 全量回填（含已有正文的，重新抓取）
  python fetch_content.py --limit 10      # 只抓前 10 条（调试）
  python fetch_content.py --dry-run       # 只统计待抓数量

抓取策略：
  - 用 playwright 真实浏览器（复用 browser_data/ 登录态），浏览器内 fetch
    /t/<slug>/<id>.json 绕过 Cloudflare（DrissionPage 与 Chrome 153 不兼容已弃用）
  - 每帖间隔 2-4s，避免被限流
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

BASE = "https://linux.do"
PROXY_DEFAULT = os.environ.get("LINUXDO_PROXY", "")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CACHE_FILE = os.path.join(DATA_DIR, "linuxdo_topics.json")
CONTENT_FILE = os.path.join(DATA_DIR, "topic_content.json")
USER_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_data")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def kill_stale_chrome(port=9222):
    """清理残留 Chrome：既清固定 9222 端口，也清 browser_data profile 实例"""
    import subprocess
    patterns = [f"--remote-debugging-port={port}", "browser_data"]
    killed = set()
    for pat in patterns:
        try:
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
            for pid in r.stdout.split():
                pid = int(pid)
                if pid in killed:
                    continue
                try:
                    os.kill(pid, 15)
                    killed.add(pid)
                    log(f"已清理残留 Chrome 进程: {pid} ({pat})")
                except Exception:
                    pass
        except Exception:
            pass


def start_browser(proxy=PROXY_DEFAULT, headless=False):
    """启动 playwright 持久化上下文（复用 browser_data 登录态，绕过 CF）"""
    from browser_utils import start_browser as _start
    ctx, page = _start(proxy=proxy, headless=headless)
    # 兼容旧接口：返回 page；context 挂在 page 上由调用方 close
    page._ctx = ctx
    return page


def check_login(pg):
    """检查是否已登录（存在 #current-user 元素）"""
    from browser_utils import check_login as _check
    try:
        return _check(pg)
    except Exception:
        return False


def make_slug(row):
    """从 url 提取 slug：/t/<slug>/<id>"""
    url = row.get("url") or ""
    m = re.match(r"/t/([^/]+)/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    return "topic", str(row.get("id"))


def strip_html(html):
    """HTML → 纯文本，压缩空白"""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def make_excerpt(text, limit=300):
    if not text:
        return ""
    return text[:limit] + ("…" if len(text) > limit else "")


def fetch_content(pg, row):
    """抓取单帖正文。返回 {content, excerpt} 或 None"""
    slug, tid = make_slug(row)
    data = pg.evaluate("""
    async (args) => {
        const {slug, tid} = args;
        try {
            const url = "/t/" + slug + "/" + tid + ".json";
            const r = await fetch(url, {credentials: "include", headers: {"Accept": "application/json"}});
            if (!r.ok) return {error: "HTTP " + r.status};
            return await r.json();
        } catch(e) { return {error: String(e)}; }
    }
    """, {"slug": slug, "tid": tid})

    if not data or data.get("error"):
        log(f"  ✗ 帖子 {tid} 抓取失败: {(data or {}).get('error', '空响应')}")
        return None

    # 正文：post_stream.posts[0].raw (Markdown) 或 .cooked (HTML)
    posts = ((data.get("post_stream") or {}).get("posts")) or []
    if not posts:
        log(f"  ✗ 帖子 {tid} 无 post_stream")
        return None
    p0 = posts[0]
    content = p0.get("raw") or p0.get("cooked") or ""
    if not content:
        log(f"  ✗ 帖子 {tid} 正文为空")
        return None
    # 若抓到的是 HTML，转纯文本
    if "<" in content:
        content = strip_html(content)
    return {
        "content": content,
        "excerpt": make_excerpt(content),
        "author_raw": p0.get("username") or "",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全量回填（重抓已有正文的）")
    ap.add_argument("--limit", type=int, default=0, help="最多抓 N 条（0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不抓取")
    ap.add_argument("--no-proxy", action="store_true", help="不走代理")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    args = ap.parse_args()

    rows = load_json(CACHE_FILE, [])
    contents = load_json(CONTENT_FILE, {})
    log(f"缓存帖子 {len(rows)} 条，已有正文 {len(contents)} 条")

    if args.all:
        todo = rows
    else:
        todo = [r for r in rows if str(r.get("id")) not in contents]
    log(f"待抓正文 {len(todo)} 条" + ("（全量回填）" if args.all else "（增量补全）"))

    if args.dry_run:
        log("dry-run 结束")
        return
    if not todo:
        log("没有需要抓取的帖子")
        return

    kill_stale_chrome()
    pg = start_browser(proxy=None if args.no_proxy else PROXY_DEFAULT, headless=args.headless)
    if not check_login(pg):
        # Linux.do 公开社区：未登录也能抓正文（playwright 真实浏览器已过 CF）。仅提示不阻断。
        log("⚠️ 未检测到登录态（公开数据仍可抓，如需登录请先运行 --browse）")
    log("浏览器就绪")

    ok = fail = 0
    for i, row in enumerate(todo):
        tid = str(row.get("id"))
        if args.limit and i >= args.limit:
            break
        time.sleep(random.uniform(2, 4))
        res = fetch_content(pg, row)
        if res:
            contents[tid] = res
            ok += 1
            if ok % 20 == 0:
                save_json(CONTENT_FILE, contents)
                log(f"  进度 {i+1}/{len(todo)}，成功 {ok}，失败 {fail}（已存盘）")
        else:
            fail += 1
        # 每 50 条存一次盘，防中断丢失
        if (i + 1) % 50 == 0:
            save_json(CONTENT_FILE, contents)

    save_json(CONTENT_FILE, contents)
    log(f"完成：成功 {ok}，失败 {fail}，正文总数 {len(contents)} → {CONTENT_FILE}")
    pg._ctx.close()


if __name__ == "__main__":
    main()
