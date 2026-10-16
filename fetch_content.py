#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_content.py — 抓取 Linux.do 帖子正文（全量回填/增量补全）

为什么独立成脚本：
  - 正文抓取是"每条一请求"，与列表抓取节奏完全不同，放主 scraper 会拖慢每日增量
  - 支持全量回填历史（--all），也支持只补没有正文的（默认）
  - 正文存本地 data/topic_content.json（{topic_id: {content, fetched_at}}）

用法：
  python fetch_content.py                 # 只补缓存里没有正文的帖子（增量）
  python fetch_content.py --all           # 全量回填（含已有正文的，重新抓取）
  python fetch_content.py --limit 10      # 只抓前 10 条（调试）
  python fetch_content.py --dry-run       # 只统计待抓数量

抓取策略：
  - 用 playwright 真实浏览器（复用 browser_data/ 登录态），浏览器内 fetch
    /t/<slug>/<id>.json 绕过 Cloudflare（DrissionPage 与 Chrome 153 不兼容已弃用）
  - 两个入口共用批处理：每帖间隔 1–2s，失败最多重试一次，每 20 条存盘
"""

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from common import PROXY_DEFAULT, load_json, log, write_json_atomic

BASE = "https://linux.do"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CACHE_FILE = os.path.join(DATA_DIR, "linuxdo_topics.json")
CONTENT_FILE = os.path.join(DATA_DIR, "topic_content.json")


def start_browser(proxy=PROXY_DEFAULT, headless=False, offscreen=True):
    """启动 playwright 持久化上下文（复用 browser_data 登录态，绕过 CF）

    offscreen=True（默认）窗口在屏幕外，不弹窗不抢焦点（headless 会被 CF 拦）。
    """
    from browser_utils import start_browser as _start
    return _start(proxy=proxy, headless=headless, offscreen=offscreen)


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


def fetch_content(pg, row):
    """抓取单帖正文。返回正文数据或 None。"""
    slug, tid = make_slug(row)
    data = pg.evaluate("""
    async (args) => {
        const {slug, tid} = args;
        try {
            const url = "/t/" + slug + "/" + tid + ".json";
            const r = await fetch(url, {credentials: "include", headers: {"Accept": "application/json"}, signal: AbortSignal.timeout(30000)});
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
        "author_raw": p0.get("username") or "",
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
    }


def has_content(entry):
    """Only a nonempty text body counts as a completed cache entry."""
    return (isinstance(entry, dict) and isinstance(entry.get('content'), str)
            and bool(entry['content'].strip()))


def fetch_batch(page, rows, contents, *, path=CONTENT_FILE, force=False,
                supplied=None, delay=(1, 2), checkpoint=20):
    """Fetch bounded batches; both entry points reuse cache and checkpoint progress.

    Missing rows remain eligible for the standalone backfill command. On a browser
    error, persist completed work and propagate it; never retry a closed context.
    """
    supplied = supplied or {}
    success = failed = reused = 0
    total = len(rows)
    try:
        for index, row in enumerate(rows, 1):
            topic_id = str(row['id'])
            result = None if force else contents.get(topic_id)
            if has_content(result):
                reused += 1
            elif topic_id in supplied:
                result = {'content': supplied[topic_id],
                          'author_raw': row.get('author', ''),
                          'fetched_at': datetime.now().isoformat(timespec='seconds')}
            else:
                for attempt in range(2):
                    time.sleep(random.uniform(*delay) if attempt == 0 else 8)
                    result = fetch_content(page, row)
                    if result:
                        break
            if has_content(result):
                contents[topic_id] = result
                row['content'] = result['content']
                success += 1
            else:
                failed += 1
            if index % checkpoint == 0 or index == total:
                write_json_atomic(path, contents)
                log(f'正文进度 {index}/{total}，成功 {success}，失败 {failed}，缓存复用 {reused}')
    finally:
        write_json_atomic(path, contents)
    return {'success': success, 'failed': failed, 'reused': reused}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全量回填（重抓已有正文的）")
    ap.add_argument("--limit", type=int, default=0, help="最多抓 N 条（0=不限）")
    ap.add_argument("--recent-days", type=int, default=0, help="只补最近 N 天发布的帖子（0=不限）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不抓取")
    ap.add_argument("--no-proxy", action="store_true", help="不走代理")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    ap.add_argument("--show-browser", action="store_true", help="显示浏览器窗口（默认离屏，不干扰桌面）")
    args = ap.parse_args()

    rows = load_json(CACHE_FILE, [])
    contents = load_json(CONTENT_FILE, {})
    log(f"缓存帖子 {len(rows)} 条，已有正文 {len(contents)} 条")

    if args.all:
        todo = rows
    else:
        todo = [r for r in rows if not has_content(contents.get(str(r.get("id"))))]

    if args.recent_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.recent_days)
        def _created(row):
            try:
                dt = datetime.fromisoformat(str(row.get("created_at", "")).replace("Z", "+00:00"))
            except ValueError:
                return None
            # 无时区的 created_at 会变成 naive datetime，与 aware 的 cutoff 比较会崩。
            # 缓存里存在这类历史行（DOM 回退路径产出），必须补 tzinfo 后再比。
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        todo = [r for r in todo if (_created(r) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
        todo.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)  # 新的优先

    log(f"待抓正文 {len(todo)} 条" + ("（全量回填）" if args.all else "（增量补全）")
        + (f"，限定最近 {args.recent_days} 天" if args.recent_days else ""))

    if args.dry_run:
        log("dry-run 结束")
        return
    if not todo:
        log("没有需要抓取的帖子")
        return

    ctx, pg = start_browser(proxy=None if args.no_proxy else PROXY_DEFAULT,
                            headless=args.headless, offscreen=not args.show_browser)
    try:
        contents = load_json(CONTENT_FILE, {})
        if not check_login(pg):
            log('⚠️ 未检测到登录态（公开数据仍可抓，如需登录请先运行 --browse）')
        targets = todo[:args.limit] if args.limit else todo
        result = fetch_batch(pg, targets, contents, path=CONTENT_FILE, force=args.all)
        log(f"完成：成功 {result['success']}，失败 {result['failed']}，正文总数 {len(contents)}")
        if result['failed']:
            raise SystemExit(1)
    finally:
        ctx.close()


if __name__ == '__main__':
    main()
