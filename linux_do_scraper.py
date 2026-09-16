#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linux_do_scraper.py — Linux.do 帖子数据抓取器（DrissionPage 真实浏览器版）

用途：
  为「帖子日报仪表盘」抓取全板块帖子数据。
  绕过 Cloudflare Turnstile 托管挑战：必须用真实 Chrome（DrissionPage 控制），
  纯 HTTP 库（requests / curl_cffi / cloudscraper）全部 403。

流程：
  1) python linux_do_scraper.py --browse    # 首次：打开浏览器，人工登录，等待登录成功后退出
  2) python linux_do_scraper.py --scrape    # 自动抓取：遍历板块 → 滚动分页 → 抓取话题 → 本地JSON
  3) 结果写入 data/linuxdo_topics.json（增量），并打印飞书多维表格插入用 JSON 行

用法：
  python linux_do_scraper.py --browse                     # 仅登录
  python linux_do_scraper.py --scrape                     # 抓取（默认全部板块）
  python linux_do_scraper.py --scrape --cats 开发调优,前沿快讯
  python linux_do_scraper.py --scrape --limit 50          # 每板块最多 50 条
  python linux_do_scraper.py --scrape --no-proxy          # 不走代理
  python linux_do_scraper.py --scrape --json-only         # 只输出飞书插入 JSON，不落盘
"""

import argparse
import json
import os
import re
import sys
import time
import random
from datetime import datetime

from DrissionPage import ChromiumOptions, ChromiumPage

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
BASE = "https://linux.do"
PROXY_DEFAULT = "127.0.0.1:7897"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CACHE_FILE = os.path.join(DATA_DIR, "linuxdo_topics.json")
BROWSER_DATA = os.path.join(os.getcwd(), "browser_data")

# 板块配置（与 linux_do_gui.py 保持一致）
CATS = [
    {"n": "开发调优", "u": "/c/develop/4", "e": True},
    {"n": "国产替代", "u": "/c/domestic/98", "e": True},
    {"n": "资源荟萃", "u": "/c/resource/14", "e": True},
    {"n": "网盘资源", "u": "/c/resource/cloud-asset/94", "e": True},
    {"n": "文档共建", "u": "/c/wiki/42", "e": True},
    {"n": "积分乐园", "u": "/c/credit/106", "e": False},
    {"n": "非我莫属", "u": "/c/job/27", "e": True},
    {"n": "读书成诗", "u": "/c/reading/32", "e": True},
    {"n": "扬帆起航", "u": "/c/startup/46", "e": False},
    {"n": "前沿快讯", "u": "/c/news/34", "e": True},
    {"n": "网络记忆", "u": "/c/feeds/92", "e": True},
    {"n": "福利羊毛", "u": "/c/welfare/36", "e": True},
    {"n": "搞七捻三", "u": "/c/gossip/11", "e": True},
    {"n": "社区孵化", "u": "/c/incubator/102", "e": False},
    {"n": "虫洞广场", "u": "/c/square/110", "e": True},
    {"n": "运营反馈", "u": "/c/feedback/2", "e": False},
]


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 浏览器
# ---------------------------------------------------------------------------
def kill_stale_chrome():
    """清理上次退出残留的 DrissionPage Chrome（占用 9222 调试端口会致新实例 WS 404）"""
    import subprocess
    try:
        out = subprocess.run(
            ["pgrep", "-f", "remote-debugging-port=9222"],
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


def start_browser(proxy=None, headless=False):
    """启动真实 Chrome（复用项目现有启动方式）"""
    kill_stale_chrome()
    co = ChromiumOptions()
    co.set_user_data_path(BROWSER_DATA)
    if proxy:
        co.set_proxy(proxy)
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-dev-shm-usage")
    if headless:
        co.set_argument("--headless=new")
    co.set_argument("--window-size=1920,1080")
    pg = ChromiumPage(co)
    return pg


def wait_cf_challenge(pg, url, timeout=60):
    """等待 Cloudflare 托管挑战完成（页面 title 不再含 'Just a moment'）"""
    pg.get(url)
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = ""
        try:
            title = pg.title or ""
        except Exception:
            pass
        if "Just a moment" not in title and "Attention Required" not in title:
            return True
        time.sleep(2)
    log(f"⚠️ Cloudflare 挑战超时（{timeout}s），当前 title: {pg.title}")
    return False


def check_login(pg):
    """检查是否已登录（存在 #current-user 元素）"""
    try:
        pg.get(BASE)
        time.sleep(3)
        return pg.ele("#current-user", timeout=8) is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def scrape_category(pg, cat, limit=0, page_delay=(2, 4), max_pages=40):
    """抓取单个板块的话题（浏览器内 fetch Discourse JSON，带登录 cookie 绕过 CF）。

    用 /c/<slug>.json 的 JSON API，page=N 分页，字段结构化（作者/回复/浏览/时间/分类）。
    若 JSON 失败则回退到 DOM 解析。
    """
    slug = cat["u"].rstrip("/").split("/")[-1]
    topics = []
    seen = set()
    page_num = 0

    while page_num < max_pages:
        page_num += 1
        time.sleep(random.uniform(*page_delay))

        # 浏览器内 fetch 同源 JSON（带 cookie 绕过 CF）
        data = pg.run_js("""
        async function fetchPage() {
            try {
                const url = "/c/%s.json?page=%d";
                const r = await fetch(url, {credentials: "include", headers: {"Accept": "application/json"}});
                if (!r.ok) return {error: "HTTP " + r.status};
                return await r.json();
            } catch(e) { return {error: String(e)}; }
        }
        return fetchPage();
        """ % (slug, page_num - 1))

        if not data or data.get("error"):
            log(f"  板块[{cat['n']}] JSON 失败({data.get('error') if data else '空'})，回退 DOM 解析")
            return scrape_category_dom(pg, cat, limit=limit, page_delay=page_delay)

        topic_list = (data.get("topic_list") or {}).get("topics") or []
        if not topic_list:
            log(f"  板块[{cat['n']}] 第{page_num}页: 无更多话题，停止")
            break

        fresh = 0
        for t in topic_list:
            tid = str(t.get("id"))
            if tid in seen:
                continue
            seen.add(tid)
            # 作者：posters[0].username（原帖人）
            author = ""
            posters = t.get("posters") or []
            if posters:
                pu = posters[0].get("username") or ""
                if not pu:
                    # 有时 posters 只给 user_id，从 users 数组反查
                    uid = posters[0].get("user_id")
                    for u in data.get("users") or []:
                        if u.get("id") == uid:
                            pu = u.get("username") or ""
                            break
                author = pu
            topics.append({
                "id": tid,
                "title": (t.get("title") or "")[:200],
                "url": t.get("slug") and f"/t/{t.get('slug')}/{tid}" or f"/t/topic/{tid}",
                "replies": int(t.get("reply_count") or 0),
                "views": int(t.get("views") or 0),
                "author": author,
                "created_at": t.get("created_at") or "",
                "bumped_at": t.get("bumped_at") or "",
            })
            fresh += 1
        log(f"  板块[{cat['n']}] 第{page_num}页: JSON 抓取 {len(topic_list)} 条，新增 {fresh} 条（累计 {len(topics)}）")

        if limit and len(topics) >= limit:
            log(f"  已达 limit {limit}，停止")
            break

        # 是否还有下一页
        if not (data.get("topic_list") or {}).get("more_topics_url"):
            break

    return topics


def scrape_category_dom(pg, cat, limit=0, page_delay=(2, 4)):
    """回退：DOM 解析版（从列表行直接抓）"""
    url = BASE + cat["u"]
    if not wait_cf_challenge(pg, url):
        return []

    topics = []
    seen = set()
    page_num = 0
    max_pages = 30

    while page_num < max_pages:
        page_num += 1
        time.sleep(random.uniform(*page_delay))

        # 用 JS 抓当前页列表（从每行 DOM 直接取字段，不依赖预加载 JSON）
        page_topics = pg.run_js("""
        function scrapePage() {
            const rows = document.querySelectorAll('tr.topic-list-item');
            const out = [];
            rows.forEach(row => {
                const link = row.querySelector('a.title.raw-link.raw-topic-link');
                if (!link) return;
                const href = link.getAttribute('href');
                const title = link.textContent.trim();
                const id = row.getAttribute('data-topic-id');
                if (!href || !title || row.classList.contains('pinned')) return;

                // 回复数: td.posts-map span.number
                let replies = 0;
                const postsTd = row.querySelector('td.posts-map .number, td.num.posts .number, td.posts .number');
                if (postsTd) {
                    const m = postsTd.textContent.replace(/[,\\s]/g, '').match(/(\\d+)/);
                    if (m) replies = parseInt(m[1]);
                }
                // 浏览量: td.views .number（可能带 k/m 缩写）
                let views = 0;
                const viewsTd = row.querySelector('td.views .number, td.num.views .number, td.views');
                if (viewsTd) {
                    const txt = viewsTd.textContent.trim();
                    const m = txt.replace(/[,\\s]/g, '').match(/^([\\d.]+)([km]?)$/i);
                    if (m) {
                        let v = parseFloat(m[1]);
                        if (m[2].toLowerCase() === 'k') v *= 1000;
                        if (m[2].toLowerCase() === 'm') v *= 1000000;
                        views = Math.round(v);
                    }
                }
                // 作者: 主帖作者 a[data-user-card]
                let author = '';
                const starter = row.querySelector('td.main-link a[data-user-card], td.posters a[data-user-card], td:nth-child(3) a[data-user-card]');
                if (starter) author = starter.getAttribute('data-user-card') || '';
                if (!author) {
                    const anyCard = row.querySelector('[data-user-card]');
                    if (anyCard) author = anyCard.getAttribute('data-user-card') || '';
                }
                // 时间: 最后活跃 a.last-posted-at 或 td.activity a，优先绝对时间 title
                let bumped_at = '';
                const lastPosted = row.querySelector('a.last-posted-at, td.activity a, td.num.activity a, td.activity');
                if (lastPosted) {
                    bumped_at = lastPosted.getAttribute('title') || lastPosted.getAttribute('datetime') || '';
                }
                if (!bumped_at) {
                    const actTd = row.querySelector('td.activity, td.num.activity');
                    if (actTd) bumped_at = actTd.getAttribute('title') || actTd.textContent.trim() || '';
                }
                // 创建时间: .link-bottom-line 内 span[title]（如 "创建日期：2026 年 9月 16 日"）或相对时间
                let created_at = '';
                const bottomLine = row.querySelector('.link-bottom-line');
                if (bottomLine) {
                    const t = bottomLine.querySelector('span[title]');
                    if (t) created_at = t.getAttribute('title') || t.textContent.trim();
                }
                if (!created_at) {
                    const b2 = row.querySelector('.link-bottom-line');
                    if (b2) created_at = b2.textContent.trim().substring(0, 60);
                }

                out.push({
                    id: id,
                    title: title.substring(0, 200),
                    url: href,
                    replies: replies,
                    views: views,
                    author: author,
                    created_at: created_at,
                    bumped_at: bumped_at
                });
            });
            return out;
        }
        return scrapePage();
        """)

        fresh = 0
        for t in page_topics or []:
            if t["id"] not in seen:
                seen.add(t["id"])
                topics.append(t)
                fresh += 1
        log(f"  板块[{cat['n']}] 第{page_num}页: 抓取 {len(page_topics or [])} 条，新增 {fresh} 条（累计 {len(topics)}）")

        if limit and len(topics) >= limit:
            log(f"  已达 limit {limit}，停止")
            break

        # 尝试点击"下一页"（Discourse 列表分页是 a[rel='next']）
        try:
            next_btn = pg.ele("a[rel='next']", timeout=2)
            if not next_btn:
                break
            next_btn.click()
            time.sleep(2)
        except Exception:
            break

    return topics


def scrape_all(pg, cats, limit=0, max_pages=40):
    """遍历所有板块抓取"""
    result = {}
    for cat in cats:
        log(f"抓取板块: {cat['n']} ({cat['u']})")
        topics = scrape_category(pg, cat, limit=limit, max_pages=max_pages)
        result[cat["n"]] = topics
        log(f"  → 板块[{cat['n']}] 共 {len(topics)} 条")
        time.sleep(random.uniform(1, 2))
    return result


def flatten(result):
    """把所有板块结果拍平，附板块名"""
    rows = []
    for cat_name, topics in result.items():
        for t in topics:
            t = dict(t)
            t["category"] = cat_name
            rows.append(t)
    return rows


def merge_incremental(all_rows):
    """与本地缓存合并（按 topic id 去重，新的优先），返回 (新增, 全部)"""
    os.makedirs(DATA_DIR, exist_ok=True)
    old = {}
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                old = {r["id"]: r for r in json.load(f)}
        except Exception as e:
            log(f"缓存读取失败（忽略）: {e}")

    new_rows = []
    for r in all_rows:
        if r["id"] not in old:
            new_rows.append(r)
    merged = list(old.values()) + new_rows
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)
    return new_rows, merged


# ---------------------------------------------------------------------------
# 自动打标
# ---------------------------------------------------------------------------
TAG_RULES = [
    ("AI模型", ["gpt", "claude", "gemini", "大模型", "chatgpt", "deepseek", "qwen", "豆包", "llm", "agent", "模型", "token", "api", "ai", "人工智能", "智能"]),
    ("教程", ["教程", "怎么", "如何", "指南", "入门", "配置", "安装", "方法"]),
    ("福利", ["免费", "领取", "白嫖", "福利", "优惠", "活动", "抽奖", "赠送"]),
    ("薅羊毛", ["羊毛", "白嫖", "薅"]),
    ("网盘", ["网盘", "阿里云盘", "夸克", "百度网盘"]),
    ("资源分享", ["资源", "下载", "分享", "合集"]),
    ("求职", ["求职", "找工作", "offer", "面试", "简历", "跳槽"]),
    ("招聘", ["招聘", "内推", "招人"]),
    ("新闻", ["快讯", "发布", "上线", "官宣", "宣布", "推出", "重磅"]),
    ("工具", ["工具", "脚本", "插件", "软件", "app", "cli", "开源"]),
    ("求助", ["求助", "帮忙", "求救", "有没有人"]),
    ("闲聊", ["闲聊", "水帖", "灌水", "大家"]),
    ("避坑", ["避坑", "注意", "警告", "别", "坑"]),
]


def auto_tags(title):
    """按标题关键词自动打标，返回标签列表（去重、保序）"""
    t = (title or "").lower()
    tags = []
    for name, kws in TAG_RULES:
        if any(k.lower() in t for k in kws) and name not in tags:
            tags.append(name)
    return tags


def tag_topic(topic):
    """给单条 topic 记录补 tags 字段"""
    topic = dict(topic)
    topic["tags"] = auto_tags(topic.get("title", ""))
    return topic


# ---------------------------------------------------------------------------
# 输出：飞书多维表格插入 JSON（每行一个 topic 记录）
# ---------------------------------------------------------------------------
def feishu_rows(rows):
    """把 topic 列表转成飞书多维表格字段格式的 JSON 行"""
    out = []
    for r in rows:
        created = r.get("created_at") or ""
        bumped = r.get("bumped_at") or ""
        # 时间戳转 ISO（Discourse 是 RFC3339；秒级时间戳则换算）
        def fmt(ts):
            if not ts:
                return ""
            try:
                if re.match(r"^\d{10,13}$", str(ts)):
                    ts = int(ts)
                    if ts > 1e12:
                        ts /= 1000
                    return datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                return ts.replace("T", " ").replace("Z", "")[:19]
            except Exception:
                return str(ts)
        out.append({
            "标题": r.get("title", ""),
            "帖子链接": BASE + r.get("url", ""),
            "作者": r.get("author", ""),
            "板块": r.get("category", ""),
            "回复数": int(r.get("replies", 0) or 0),
            "浏览量": int(r.get("views", 0) or 0),
            "发布时间": fmt(created),
            "最近活跃": fmt(bumped),
            "Topic ID": r.get("id", ""),
            "标签": r.get("tags") or [],
        })
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Linux.do 帖子抓取器")
    ap.add_argument("--browse", action="store_true", help="仅打开浏览器等待人工登录")
    ap.add_argument("--scrape", action="store_true", help="抓取帖子数据")
    ap.add_argument("--cats", default="", help="板块名逗号分隔，默认全部启用板块")
    ap.add_argument("--limit", type=int, default=0, help="每板块最多抓取条数（0=不限）")
    ap.add_argument("--recent", action="store_true", help="近期模式：每板块只抓前3页(约90条)，默认开启")
    ap.add_argument("--full", action="store_true", help="全量模式：抓每个板块全部分页")
    ap.add_argument("--no-proxy", action="store_true", help="不使用代理")
    ap.add_argument("--json-only", action="store_true", help="只输出飞书插入 JSON，不落盘")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    args = ap.parse_args()

    if not args.browse and not args.scrape:
        ap.print_help()
        sys.exit(0)

    proxy = None if args.no_proxy else PROXY_DEFAULT
    log(f"启动浏览器（proxy={proxy or '无'}）...")
    pg = start_browser(proxy=proxy, headless=args.headless)

    if args.browse:
        log("请在浏览器中登录 Linux.do（如已登录可忽略）")
        if check_login(pg):
            log("✅ 已登录")
        else:
            log("等待登录...（最多 300s）")
            deadline = time.time() + 300
            while time.time() < deadline:
                if check_login(pg):
                    log("✅ 登录成功")
                    break
                time.sleep(5)
            else:
                log("⚠️ 等待登录超时")
        pg.quit()
        sys.exit(0)

    if args.scrape:
        if not check_login(pg):
            log("❌ 未登录。请先运行: python linux_do_scraper.py --browse")
            pg.quit()
            sys.exit(1)

        if args.cats:
            wanted = [c.strip() for c in args.cats.split(",")]
            cats = [c for c in CATS if c["n"] in wanted]
        else:
            cats = [c for c in CATS if c["e"]]

        log(f"开始抓取 {len(cats)} 个板块...")
        # 模式：--full 全量分页；--recent 或默认 → 每板块前3页（近期活跃）
        if args.full:
            max_pages = 40
        else:
            max_pages = 3
        result = scrape_all(pg, cats, limit=args.limit, max_pages=max_pages)
        all_rows = flatten(result)

        # 自动打标
        for row in all_rows:
            row["tags"] = auto_tags(row.get("title", ""))
        log(f"打标完成（含标签的帖子: {sum(1 for r in all_rows if r['tags'])}/{len(all_rows)}）")

        log(f"共抓取 {len(all_rows)} 条帖子")

        # 无论 --json-only 与否，都先落盘增量缓存（保证增量语义一致，避免重复入飞书）
        new_rows, merged = merge_incremental(all_rows)
        log(f"本地缓存: 新增 {len(new_rows)} 条，累计 {len(merged)} 条 → {CACHE_FILE}")

        rows = feishu_rows(new_rows)
        print(json.dumps(rows, ensure_ascii=False))
        log(f"飞书待插入 {len(rows)} 条（增量）。可用 lark-cli base +record-create 写入。")

        pg.quit()
        sys.exit(0)


if __name__ == "__main__":
    main()
