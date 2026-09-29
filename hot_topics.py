#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hot_topics.py — Linux.do 官方热榜抓取（/top.json?period=daily|weekly|monthly）

为什么单独做：Discourse 的 /top.json 就是社区自己按周期算出的热榜，
一次请求拿 50 条，带浏览量/回复数/点赞数，比从板块列表里自己估热度准得多。

用法：
  python hot_topics.py                          # 日榜 + 周榜，落盘 data/hot_topics.json
  python hot_topics.py --periods daily --limit 20
  python hot_topics.py --json                   # 只打印 JSON（供管道使用）

被 linux_do_scraper.py --hot 复用：复用已开启的浏览器会话，不额外启动 Chrome。
"""

import argparse
import json
import os
import sys
from datetime import datetime

BASE = "https://linux.do"
PROXY_DEFAULT = os.environ.get("LINUXDO_PROXY", "")
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
HOT_FILE = os.path.join(DATA_DIR, "hot_topics.json")
CATEGORY_FILE = os.path.join(DATA_DIR, "category_map.json")
PERIODS = ("daily", "weekly", "monthly")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def load_category_map(pg=None):
    """category_id -> 板块名；优先读本地缓存，没有就用已开启的浏览器刷新。"""
    try:
        with open(CATEGORY_FILE, "r", encoding="utf-8") as f:
            cached = json.load(f)
        if cached:
            return cached
    except (OSError, ValueError):
        pass
    if pg is None:
        return {}
    site = pg.evaluate("""async () => {
        const r = await fetch('/site.json', {credentials: 'include', headers: {'Accept': 'application/json'}});
        return r.ok ? await r.json() : {};
    }""")
    mapping = {str(c.get("id")): c.get("name", "") for c in (site.get("categories") or [])}
    if mapping:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CATEGORY_FILE, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=1)
    return mapping


def fetch_period(pg, period):
    """抓取单个周期的热榜原始 topic 列表。"""
    data = pg.evaluate("""async (period) => {
        const r = await fetch('/top.json?period=' + period,
            {credentials: 'include', headers: {'Accept': 'application/json'}});
        if (!r.ok) return {error: r.status};
        const j = await r.json();
        return {topics: (j.topic_list && j.topic_list.topics) || []};
    }""", period)
    if data.get("error"):
        raise RuntimeError(f"/top.json?period={period} 返回 HTTP {data['error']}")
    return data.get("topics") or []


def normalize(raw, category_names=None):
    """转成与 linux_do_scraper 一致的字段，便于合并进缓存或直接展示。"""
    category_names = category_names or {}
    out = []
    for t in raw:
        tid = str(t.get("id", ""))
        posters = t.get("posters") or []
        author = (posters[0].get("username") or "") if posters else ""
        slug = t.get("slug") or ""
        out.append({
            "id": tid,
            "title": (t.get("title") or "")[:200],
            "url": f"/t/{slug}/{tid}" if slug else f"/t/topic/{tid}",
            "replies": int(t.get("reply_count") or 0),
            "views": int(t.get("views") or 0),
            "author": author,
            "created_at": t.get("created_at") or "",
            "bumped_at": t.get("bumped_at") or "",
            "slug": slug,
            "like_count": int(t.get("like_count") or 0),
            "posts_count": int(t.get("posts_count") or 0),
            "posters": [p.get("username") or "" for p in posters],
            "last_poster": (posters[-1].get("username") or "") if posters else "",
            "category_id": t.get("category_id") or "",
            "category": category_names.get(str(t.get("category_id")), ""),
            "tags_raw": [x for x in (t.get("tags") or []) if isinstance(x, str)],
            "pinned": bool(t.get("pinned")),
            "visible": bool(t.get("visible")),
        })
    return out


def collect(pg, periods=("daily", "weekly"), limit=0):
    """抓多个周期，返回 {period: [topic, ...]} 形态的完整 payload。"""
    category_names = load_category_map(pg)
    result = {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    for period in periods:
        try:
            topics = normalize(fetch_period(pg, period), category_names)
        except Exception as exc:
            log(f"⚠️ 热榜[{period}]抓取失败: {exc}")
            topics = []
        if limit:
            topics = topics[:limit]
        result[period] = topics
        log(f"热榜[{period}] {len(topics)} 条")
    return result


def save(payload):
    os.makedirs(DATA_DIR, exist_ok=True)
    temporary = HOT_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(temporary, HOT_FILE)
    return HOT_FILE


def load():
    try:
        with open(HOT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def print_report(payload, periods, limit=20):
    for period in periods:
        topics = payload.get(period) or []
        print(f"\n===== {period} 热榜（{len(topics)} 条）=====")
        for i, t in enumerate(topics[:limit], 1):
            print(f"{i:>2}. 👁{t['views']:>6} 💬{t['replies']:>4} ❤{t['like_count']:>4} "
                  f"[{t.get('category', '')}] {t['title']}")
            print(f"    https://linux.do{t['url']}")


def main():
    ap = argparse.ArgumentParser(description="Linux.do 官方热榜抓取")
    ap.add_argument("--periods", default="daily,weekly", help="daily,weekly,monthly 逗号分隔")
    ap.add_argument("--limit", type=int, default=0, help="每周期最多保留 N 条（0=全部）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON，不打印榜单")
    ap.add_argument("--no-proxy", action="store_true", help="不使用代理")
    ap.add_argument("--show-browser", action="store_true", help="显示浏览器窗口（默认离屏）")
    args = ap.parse_args()

    periods = [p.strip() for p in args.periods.split(",") if p.strip() in PERIODS]
    if not periods:
        ap.error(f"--periods 只支持 {','.join(PERIODS)}")

    from browser_utils import start_browser, wait_json_ready

    proxy = None if args.no_proxy else PROXY_DEFAULT
    ctx, pg = start_browser(proxy=proxy, headless=False, offscreen=not args.show_browser)
    try:
        if not wait_json_ready(pg, timeout=90):
            log("⚠️ CF 未就绪，仍继续尝试")
        payload = collect(pg, periods, limit=args.limit)
    finally:
        ctx.close()

    path = save(payload)
    log(f"已写入 {path}")
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print_report(payload, periods)
        print(f"\n已保存 → {path}")


if __name__ == "__main__":
    main()
