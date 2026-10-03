#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_replies.py — 抓取 linux.do 帖子内的回复（topic 里的 post）

与 fetch_content.py 的分工：
  - fetch_content.py  只取**首帖正文**（post_stream.posts[0]）
  - fetch_replies.py  逐帖点进去，按页拉 post_stream，取回**该帖下所有回复**

用法：
  python fetch_replies.py --recent-days 3            # 抓最近 3 天发布的帖子的回复
  python fetch_replies.py --recent-days 3 --limit 10 # 只抓前 10 条（调试）
  python fetch_replies.py --all                      # 缓存里全部帖子（量大，慎用）
  python fetch_replies.py --dry-run                  # 只统计待抓数量，不抓

输出：data/topic_replies.json
  { "<topic_id>": {"posts_count": n, "fetched": m, "fetched_at": "...",
                   "posts": [{"post_number","username","created_at","reply_to_post_number","content"}]} }

抓取策略：
  - playwright 真实浏览器（复用 browser_data/ 登录态），浏览器内 fetch
    /t/<slug>/<id>.json?page=N，绕开 Cloudflare 与跨域
  - 每帖页间 1.5-3s、帖间 2-4s，避免限流
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

BASE = "https://linux.do"
PROXY_DEFAULT = os.environ.get("LINUXDO_PROXY", "")
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
CACHE_FILE = os.path.join(DATA_DIR, "linuxdo_topics.json")
REPLIES_FILE = os.path.join(DATA_DIR, "topic_replies.json")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def kill_stale_chrome():
    """清理以 browser_data 为 profile 的残留 Chrome（会锁住登录态）"""
    for pat in ("browser_data",):
        try:
            r = subprocess.run(["pgrep", "-f", pat], capture_output=True, text=True)
            for pid in r.stdout.split():
                try:
                    os.kill(int(pid), 15)
                except Exception:
                    pass
        except Exception:
            pass
    time.sleep(2)


def start_browser(proxy=None, headless=False, offscreen=True):
    from browser_utils import start_browser as _start
    return _start(proxy=proxy, headless=headless, offscreen=offscreen)


def check_login(pg):
    """优先用 /session/current.json 判登录（稳），失败再退回 DOM 检查。返回 (ok, user)。"""
    try:
        from browser_utils import check_session as _sess
        ok, user = _sess(pg)
        if ok:
            return True, user
    except Exception:
        pass
    from browser_utils import check_login as _check
    try:
        return _check(pg), None
    except Exception:
        return False, None


def make_slug(row):
    url = row.get("url") or ""
    m = re.match(r"/t/([^/]+)/(\d+)", url)
    if m:
        return m.group(1), m.group(2)
    return "topic", str(row.get("id"))


def strip_html(html):
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


class RateLimited(Exception):
    """站点返回 429 —— 必须退避，硬冲只会把限制窗口越拉越长。"""


# 浏览器内按页拉 topic JSON
_JS_PAGE = """
async (args) => {
    const {slug, tid, page} = args;
    try {
        const url = "/t/" + slug + "/" + tid + ".json?page=" + page;
        const r = await fetch(url, {credentials: "include", headers: {"Accept": "application/json"}});
        if (!r.ok) return {error: "HTTP " + r.status};
        const j = await r.json();
        const ps = (j.post_stream || {});
        return {
            posts_count: j.posts_count || 0,
            stream_len: (ps.stream || []).length,
            posts: ps.posts || []
        };
    } catch (e) { return {error: String(e)}; }
}
"""


def fetch_replies(pg, row, max_pages=5, page_pause=(2.5, 5.0)):
    """逐页拉取指定帖子的回复。返回 dict；遇 429 抛 RateLimited。"""
    slug, tid = make_slug(row)
    posts = []
    seen = set()
    posts_count = 0
    for page in range(1, max_pages + 1):
        data = pg.evaluate(_JS_PAGE, {"slug": slug, "tid": tid, "page": page})
        if data and "429" in str(data.get("error", "")):
            raise RateLimited(f"帖子 {tid} 第 {page} 页 429")
        if not data or data.get("error"):
            if page == 1:
                log(f"  ✗ 帖子 {tid} 抓取失败: {(data or {}).get('error', '空响应')}")
                return None
            break
        posts_count = data.get("posts_count") or posts_count
        batch = data.get("posts") or []
        if not batch:
            break
        fresh = 0
        for p in batch:
            pid = p.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            fresh += 1
            posts.append({
                "post_number": p.get("post_number"),
                "username": p.get("username") or "",
                "created_at": p.get("created_at") or "",
                "reply_to_post_number": p.get("reply_to_post_number"),
                "content": strip_html(p.get("cooked") or p.get("raw") or ""),
            })
        # 第一页就含首帖，其余为回复
        if fresh == 0:
            break
        # stream 已读完（首帖 + 回复全部到手）
        if data.get("stream_len") and len(seen) >= data["stream_len"]:
            break
        time.sleep(random.uniform(*page_pause))
    # Discourse 的 posts_count 含首帖，fetched 也是含首帖的总数
    return {
        "posts_count": posts_count,
        "reply_count": max(0, posts_count - 1),
        "fetched": len(posts),
        "complete": bool(posts_count) and len(posts) >= posts_count,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "posts": posts,
    }


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(temporary, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="缓存里全部帖子（量大）")
    ap.add_argument("--recent-days", type=int, default=3, help="只抓最近 N 天发布的帖子（默认 3）")
    ap.add_argument("--limit", type=int, default=0, help="最多抓 N 条（0=不限）")
    ap.add_argument("--max-pages", type=int, default=5, help="每帖最多拉 N 页回复（每页≈20 条，默认 5）")
    ap.add_argument("--min-replies", type=int, default=0, help="只抓回复数 ≥N 的帖（默认 0=不限；限流时建议设 1）")
    ap.add_argument("--sort", choices=("time", "replies"), default="time", help="排序：time=新帖优先（默认），replies=回复多的优先")
    ap.add_argument("--cats", default="", help="只抓指定板块（逗号分隔，匹配板块名）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不抓取")
    ap.add_argument("--no-proxy", action="store_true", help="不走代理")
    ap.add_argument("--headless", action="store_true", help="无头模式（会被 CF 拦，慎用）")
    ap.add_argument("--show-browser", action="store_true", help="显示浏览器窗口")
    ap.add_argument("--pace", default="4,8", help="帖间停顿秒数区间，如 '4,8'（默认 4~8）")
    ap.add_argument("--backoff", type=float, default=60.0, help="429 首次退避秒数（默认 60，逐次翻倍）")
    ap.add_argument("--backoff-max", type=float, default=600.0, help="429 退避上限秒数（默认 600）")
    args = ap.parse_args()
    args.pace = tuple(float(x) for x in str(args.pace).split(","))

    rows = load_json(CACHE_FILE, [])
    replies = load_json(REPLIES_FILE, {})
    log(f"缓存帖子 {len(rows)} 条，已有回复档 {len(replies)} 条")

    todo = rows if args.all else None
    if not args.all and args.recent_days > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=args.recent_days)

        def _created(row):
            try:
                return datetime.fromisoformat(str(row.get("created_at", "")).replace("Z", "+00:00"))
            except ValueError:
                return None

        todo = [r for r in rows if (_created(r) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]
    if todo is None:
        todo = rows

    if args.cats:
        wanted = {c.strip() for c in args.cats.split(",") if c.strip()}
        todo = [r for r in todo if (r.get("category") or "") in wanted]

    # 默认跳过已抓过的（除非 --all）
    if not args.all:
        todo = [r for r in todo if str(r.get("id")) not in replies]

    # 限流之下，请求额要花在有内容的帖上：回复数门槛 + 多的优先
    if args.min_replies > 0:
        todo = [r for r in todo if int(r.get("replies") or 0) >= args.min_replies]

    if args.sort == "replies":
        todo.sort(key=lambda r: (-int(r.get("replies") or 0), str(r.get("created_at", ""))), reverse=False)
    else:
        todo.sort(key=lambda r: str(r.get("created_at", "")), reverse=True)  # 新的优先

    log(f"待抓回复 {len(todo)} 条"
        + (f"（最近 {args.recent_days} 天）" if args.recent_days and not args.all else "（全部）")
        + (f"，限定板块 {args.cats}" if args.cats else ""))

    if args.dry_run:
        log("dry-run 结束")
        return
    if not todo:
        log("没有需要抓取的帖子")
        return

    kill_stale_chrome()

    def spin_up():
        return start_browser(proxy=None if args.no_proxy else PROXY_DEFAULT,
                             headless=args.headless,
                             offscreen=not getattr(args, "show_browser", False))

    ctx, pg = spin_up()
    logged, who = check_login(pg)
    if logged:
        log(f"✅ 登录态正常（{who or '已登录'}）")
    else:
        log("⚠️ 未检测到登录态（公开帖仍可抓，但私有/设置类内容可能缺失）")
    log("浏览器就绪")

    ok = fail = total_replies = 0
    rate_hits = 0
    rebuilds = 0
    try:
        for i, row in enumerate(todo):
            if args.limit and i >= args.limit:
                break
            tid = str(row.get("id"))
            time.sleep(random.uniform(*args.pace))
            res = None
            exhausted = False
            rebuilt_this = False
            for attempt in range(6):
                try:
                    res = fetch_replies(pg, row, max_pages=args.max_pages)
                    break
                except RateLimited as e:
                    rate_hits += 1
                    wait = min(args.backoff * (2 ** attempt), args.backoff_max)
                    wait *= random.uniform(0.9, 1.15)
                    log(f"  ⏳ 429 限流（累计 {rate_hits} 次）：{e}；退避 {wait:.0f}s 后重试")
                    time.sleep(wait)
                except Exception as e:  # noqa: BLE001
                    # 浏览器被关掉 / 崩了（TargetClosedError 等）：重建后再继续，
                    # 不要因为一次掉线就废掉整轮（曾经每轮只能跑 3-10 分钟）。
                    rebuilds += 1
                    rebuilt_this = True
                    log(f"  ⚠️ 浏览器异常（第 {rebuilds} 次重建）："
                        f"{type(e).__name__}: {str(e)[:70]}")
                    try:
                        ctx.close()
                    except Exception:  # noqa: BLE001
                        pass
                    time.sleep(5)
                    kill_stale_chrome()
                    ctx, pg = spin_up()
                    log("  浏览器已重建，继续")
                    break
            else:
                exhausted = True
            if exhausted:
                log(f"  ✗ 帖子 {tid} 连续 429，暂停本轮（已存进度，可续跑）")
                break
            if res:
                replies[tid] = res
                ok += 1
                total_replies += res.get("reply_count", 0)
            elif not rebuilt_this:
                fail += 1
            if ok % 5 == 0 or (i + 1) % 20 == 0:
                save_json(REPLIES_FILE, replies)
                log(f"  进度 {i+1}/{len(todo)}，成功 {ok}，失败 {fail}，"
                    f"回复累计 {total_replies}，429 {rate_hits}，重建 {rebuilds}")
            if (i + 1) % 20 == 0:
                save_json(REPLIES_FILE, replies)
    finally:
        save_json(REPLIES_FILE, replies)
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass
        log(f"本轮收尾：已存 {len(replies)} 帖 → {REPLIES_FILE}")

    log(f"完成：帖子 {ok} 条成功 / {fail} 失败，抓到回复 {total_replies} 条 → {REPLIES_FILE}")


if __name__ == "__main__":
    main()
