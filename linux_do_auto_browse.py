#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Export a Linux.do topic for offline reading (read-only).

This replaces the retired random-scroll / random-like bot. It fetches the
topic's Discourse JSON through the project's persistent Chrome profile and
writes a Markdown copy. It does not click like/reply/bookmark controls or
automatically visit a list of topics.

Examples:
    .venv/bin/python linux_do_auto_browse.py --topic https://linux.do/t/topic/2975832
    .venv/bin/python linux_do_auto_browse.py --topic 2975832 --limit 20

The browser profile is ``browser_data/`` (separate from the user's main Chrome).
Use ``--show-browser`` to make it visible for a one-time manual login.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

BASE_URL = "https://linux.do"
EXPORT_DIR = Path(__file__).resolve().parent / "exports"


def topic_ref_from(value: str) -> tuple[str, str]:
    """Accept either a Linux.do topic URL or a numeric topic id."""
    value = value.strip()
    if value.isdigit():
        return "topic", value
    parsed = urlparse(value)
    match = re.search(r"/t/([^/]+)/(\d+)(?:/|$)", parsed.path)
    if not match or parsed.hostname not in {"linux.do", "www.linux.do"}:
        raise ValueError("--topic must be a topic ID or a linux.do topic URL")
    return match.group(1), match.group(2)


def plain_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value or "", flags=re.I)
    value = re.sub(r"</(?:p|div|li|blockquote|h[1-6])\s*>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    return re.sub(r"[ \t]+", " ", value).strip()


def markdown_for_topic(data: dict, limit: int = 0) -> str:
    details = data.get("topic") or data
    stream = data.get("post_stream") or {}
    posts = stream.get("posts") or []
    if limit:
        posts = posts[:limit]
    title = details.get("title") or "Linux.do topic"
    lines = [f"# {title}", "", f"- Topic ID: {details.get('id', '')}",
             f"- Category: {details.get('category_id', '')}",
             f"- Exported: {datetime.now().astimezone().isoformat(timespec='seconds')}",
             f"- Posts exported: {len(posts)} / {details.get('posts_count', len(posts))}", ""]
    for post in posts:
        content = post.get("raw") or post.get("cooked") or ""
        if "<" in content:
            content = plain_text(content)
        lines.extend([f"## Post #{post.get('post_number', '?')} · {post.get('username', 'unknown')}",
                      "", content.strip(), ""])
    return "\n".join(lines).rstrip() + "\n"


def fetch_topic(page, slug: str, topic_id: str) -> dict:
    """Fetch one topic JSON using the same-origin logged-in browser context."""
    return page.evaluate("""async (args) => {
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 30000);
        const r = await fetch(`/t/${args.slug}/${args.id}.json`, {
          credentials: 'include', headers: {Accept: 'application/json'}, signal: controller.signal
        });
        clearTimeout(timer);
        if (!r.ok) return {error: `HTTP ${r.status}`};
        return await r.json();
      } catch (e) { return {error: String(e)}; }
    }""", {"slug": slug, "id": topic_id})


def fetch_latest_refs(page, count: int) -> list[tuple[str, str]]:
    """Read the live latest list, paging until count distinct topics are found."""
    refs = []
    seen = set()
    for page_number in range((count // 20) + 10):
        data = page.evaluate("""async (pageNumber) => {
          const r = await fetch(`/latest.json?page=${pageNumber}`, {
            credentials: 'include', headers: {Accept: 'application/json'}
          });
          if (!r.ok) return {error: `HTTP ${r.status}`};
          return await r.json();
        }""", page_number)
        if data.get("error"):
            raise RuntimeError(f"latest list page {page_number}: {data['error']}")
        topics = (data.get("topic_list") or {}).get("topics") or []
        if not topics:
            break
        for item in topics:
            topic_id = str(item.get("id") or "")
            if not topic_id or topic_id in seen:
                continue
            seen.add(topic_id)
            refs.append((item.get("slug") or "topic", topic_id))
            if len(refs) == count:
                return refs
        time.sleep(0.5)
    raise RuntimeError(f"live latest list returned only {len(refs)} distinct topics; requested {count}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export Linux.do topics as Markdown (read-only).")
    parser.add_argument("--topic", action="append", default=[], help="Linux.do topic ID or URL; repeat for multiple topics")
    parser.add_argument("--from-hot", type=int, default=0, metavar="N", help="also export N distinct topics from the cached daily hot list")
    parser.add_argument("--from-cache", type=int, default=0, metavar="N", help="also export N recent distinct topics from data/linuxdo_topics.json")
    parser.add_argument("--latest", type=int, default=0, metavar="N", help="export N topics from the live latest list")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between topic requests (default: 1)")
    parser.add_argument("--limit", type=int, default=0, help="maximum posts to export (0 = all returned by topic JSON)")
    parser.add_argument("--output", type=Path, help="output Markdown path (default: exports/<topic-id>.md)")
    parser.add_argument("--no-proxy", action="store_true", help="do not use LINUXDO_PROXY")
    parser.add_argument("--show-browser", action="store_true", help="show the project's browser_data Chrome profile")
    args = parser.parse_args(argv)
    if args.limit < 0 or args.from_hot < 0 or args.from_cache < 0 or args.latest < 0 or args.delay < 0:
        parser.error("numeric arguments must be zero or greater")
    if args.output and (args.from_hot or args.from_cache or args.latest or len(args.topic) != 1):
        parser.error("--output requires exactly one --topic")
    refs = []
    try:
        refs = [topic_ref_from(value) for value in args.topic]
        if args.from_hot:
            hot_path = Path(__file__).resolve().parent / "data" / "hot_topics.json"
            hot = json.loads(hot_path.read_text(encoding="utf-8"))
            refs.extend(topic_ref_from(str(item["id"])) for item in hot.get("daily", [])[:args.from_hot])
        if args.from_cache:
            cache_path = Path(__file__).resolve().parent / "data" / "linuxdo_topics.json"
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            recent = sorted(cached, key=lambda item: item.get("created_at") or "", reverse=True)
            for item in recent:
                ref = topic_ref_from(str(item["id"]))
                if ref not in refs:
                    refs.append(ref)
                if len(refs) >= len(args.topic) + args.from_hot + args.from_cache:
                    break
    except (ValueError, KeyError, OSError) as exc:
        parser.error(f"invalid topic or hot list: {exc}")
    refs = list(dict.fromkeys(refs))
    if not refs and not args.latest:
        parser.error("provide --topic or --from-hot")

    from browser_utils import PROXY_DEFAULT, start_browser

    context = None
    try:
        context, page = start_browser(proxy=None if args.no_proxy else PROXY_DEFAULT,
                                      headless=False, offscreen=not args.show_browser)
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
        # Allow an already-authenticated profile / Cloudflare clearance to settle.
        time.sleep(2)
        if args.latest:
            live_refs = fetch_latest_refs(page, args.latest)
            refs = list(dict.fromkeys(refs + live_refs))
            print(f"Fetched {len(live_refs)} distinct topics from live /latest.json", flush=True)
        failed = 0
        for index, (slug, topic_id) in enumerate(refs, 1):
            if index > 1:
                time.sleep(args.delay)
            try:
                for attempt in range(3):
                    data = fetch_topic(page, slug, topic_id)
                    if data and data.get("error") in {"HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504"} and attempt < 2:
                        time.sleep(2 ** (attempt + 1))
                        continue
                    break
                if not data or data.get("error"):
                    raise RuntimeError((data or {}).get("error", "empty response"))
                if not (data.get("post_stream") or {}).get("posts"):
                    raise RuntimeError("topic response had no posts")
                output = args.output or (EXPORT_DIR / f"{topic_id}.md")
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(markdown_for_topic(data, args.limit), encoding="utf-8")
                print(f"[{index}/{len(refs)}] Exported topic {topic_id}: {len(data['post_stream']['posts'][:args.limit or None])} posts -> {output}", flush=True)
            except Exception as exc:
                failed += 1
                print(f"Could not fetch topic {topic_id}: {exc}", file=sys.stderr, flush=True)
        print(f"Completed: {len(refs) - failed}/{len(refs)} topics", flush=True)
        return 1 if failed else 0
    finally:
        if context is not None:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
