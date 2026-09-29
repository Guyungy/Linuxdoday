#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_report.py — 生成 Linux.do 的 AI 日报（分析 + 总结）

数据来源全部是本地抓取产物，不额外打 linux.do：
  data/linuxdo_topics.json   帖子与指标（浏览量/回复数/点赞）
  data/topic_content.json    帖子正文
  data/hot_topics.json       官方热榜（/top.json daily、weekly）

分析部分由 OpenAI 兼容网关生成；数字部分由本地确定性计算后作为附录附上，
避免模型编造数据。

用法：
  python daily_report.py                              # 今天的日报（写在 reports/）
  python daily_report.py --date 2026-09-28
  python daily_report.py --no-llm                     # 不调模型，只出统计版
  python daily_report.py --push-chat oc_xxxxxxxx      # 生成后推送到飞书群

凭据：默认从 ~/.dsh/.credentials.yaml 读 DEEPSEEK_API_KEY，
      也可用环境变量 DAILY_REPORT_API_KEY / DAILY_REPORT_BASE_URL / DAILY_REPORT_MODEL 覆盖。
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
REPORT_DIR = os.path.join(ROOT, "reports")
CACHE_FILE = os.path.join(DATA_DIR, "linuxdo_topics.json")
CONTENT_FILE = os.path.join(DATA_DIR, "topic_content.json")
HOT_FILE = os.path.join(DATA_DIR, "hot_topics.json")
CRED_FILE = os.path.expanduser("~/.dsh/.credentials.yaml")

DEFAULT_BASE_URL = os.environ.get("DAILY_REPORT_BASE_URL", "https://tokenrhythm.studio/v1")
DEFAULT_MODEL = os.environ.get("DAILY_REPORT_MODEL", "deepseek-flash")
TZ = timezone(timedelta(hours=8))

THEMES = {
    "模型版本/发布": ["sonnet", "claude", "opus", "gpt", "openai", "astra", "grok", "gemini", "qwen", "deepseek", "glm", "模型"],
    "降智/额度/封号": ["降智", "额度", "封号", "限流", "overloaded", "退款", "封禁"],
    "福利/羊毛": ["抽奖", "福利", "免费", "白嫖", "羊毛", "赠送", "领取", "重置卡"],
    "开发/工具/开源": ["开源", "github", "工具", "脚本", "插件", "部署", "docker", "代码"],
    "教程/求助": ["教程", "怎么", "如何", "求助", "请问", "大佬", "有没有"],
    "账号/网络环境": ["账号", "谷歌", "封号", "梯子", "clash", "节点", "代理", "扫码"],
    "职场/生活": ["面试", "offer", "工作", "毕业", "求职", "工资", "加班"],
}


def log(msg):
    print(f"[{datetime.now(TZ).strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(TZ)
    except (TypeError, ValueError):
        return None


def read_api_key():
    key = os.environ.get("DAILY_REPORT_API_KEY")
    if key:
        return key.strip()
    try:
        with open(CRED_FILE, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    match = re.search(r"DEEPSEEK_API_KEY:\s*(\S+)", text)
    return match.group(1).strip("\"'") if match else ""


def collect(day, days=3):
    """汇总截止 day（CST）的最近 days 天帖子、指标与关键词主题。

    同时统计「上一个等长窗口」的新帖量，用于给出环比。
    """
    posts = load_json(CACHE_FILE, [])
    contents = load_json(CONTENT_FILE, {})
    hot = load_json(HOT_FILE, {})

    start = day - timedelta(days=max(days, 1) - 1)
    prev_start = start - timedelta(days=max(days, 1))
    prev_end = start - timedelta(days=1)

    rows, prev_posts = [], 0
    for p in posts:
        created = parse_time(p.get("created_at"))
        if not created:
            continue
        created_day = created.date()
        if created_day < start or created_day > day:
            if prev_start <= created_day <= prev_end:
                prev_posts += 1
            continue
        body = ((contents.get(str(p.get("id"))) or {}).get("content") or "").strip()
        rows.append({
            "id": str(p.get("id")),
            "title": (p.get("title") or "").strip(),
            "category": p.get("category") or "",
            "author": p.get("author") or "",
            "views": int(p.get("views") or 0),
            "replies": int(p.get("replies") or 0),
            "likes": int(p.get("like_count") or 0),
            "time": created.strftime("%m-%d %H:%M"),
            "url": f"https://linux.do/t/topic/{p.get('id')}",
            "content": body,
        })
    rows.sort(key=lambda r: r["views"] + r["replies"] * 20, reverse=True)

    with_content = sum(1 for r in rows if r["content"])
    overview = {
        "date": day.isoformat(),
        "days": max(days, 1),
        "start": start.isoformat(),
        "end": day.isoformat(),
        "prev_posts": prev_posts,
        "posts": len(rows),
        "with_content": with_content,
        "views": sum(r["views"] for r in rows),
        "replies": sum(r["replies"] for r in rows),
        "categories": dict(Counter(r["category"] for r in rows).most_common()),
    }
    tiers = []
    for lo, hi, label in [(1000, 10**9, "爆款(≥1000)"), (300, 1000, "热门(300-1000)"),
                          (100, 300, "中等(100-300)"), (0, 100, "长尾(<100)")]:
        group = [r for r in rows if lo <= r["views"] < hi]
        tiers.append({"label": label, "count": len(group), "views": sum(r["views"] for r in group)})
    overview["tiers"] = tiers

    blob = [(r["title"] + " " + r["content"]).lower() for r in rows]
    themes = []
    for name, kws in THEMES.items():
        n = sum(1 for text in blob if any(k in text for k in kws))
        themes.append({"name": name, "posts": n, "share": round(n / max(len(rows), 1) * 100)})
    overview["themes"] = sorted(themes, key=lambda t: -t["posts"])

    return rows, overview, hot


def assign_themes(rows):
    """把每篇帖子归到命中关键词最多的主题；每帖只归一个主题，避免汇总里重复出现。"""
    buckets = {name: [] for name in THEMES}
    for r in rows:
        text = (r["title"] + " " + r["content"]).lower()
        best, best_score = None, 0
        for name, kws in THEMES.items():
            score = sum(1 for k in kws if k in text)
            if score > best_score:
                best, best_score = name, score
        if best:
            buckets[best].append(r)
    return {name: items for name, items in buckets.items() if items}


def render_topics(rows, overview, per_theme=3):
    """话题汇总：按主题聚类 + 代表帖（程序确定性生成，不依赖模型）。"""
    buckets = assign_themes(rows)
    total = max(len(rows), 1)
    lines = ["## 📌 话题汇总", ""]
    ordered = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    for name, items in ordered:
        share = round(len(items) / total * 100)
        lines.append(f"**{name}**（{len(items)} 帖 · {share}%）")
        for r in items[:per_theme]:
            lines.append(f"- [{r['title']}]({r['url']}) · {r['category']} · 👁{r['views']} 💬{r['replies']}")
        lines.append("")
    return "\n".join(lines).strip()


def render_hot(rows, hot, top):
    """未被任何主题命中、但热度最高的帖子（补充榜）。"""
    lines = [f"## 🔎 其它高热帖（Top {min(top, len(rows))}）", ""]
    for i, r in enumerate(rows[:top], 1):
        lines.append(f"{i}. [{r['title']}]({r['url']}) · {r['category']} · 👁{r['views']} 💬{r['replies']} ❤{r['likes']}")
    daily = (hot.get("daily") or [])[:10]
    if daily:
        lines.append("")
        lines.append("**官方日榜 Top 10**")
        lines.append("")
        for t in daily:
            lines.append(f"- [{t.get('title', '')}](https://linux.do{t.get('url', '')}) · "
                         f"{t.get('category', '')} · 👁{t.get('views', 0)} 💬{t.get('replies', 0)}")
    return "\n".join(lines)


def build_prompt(rows, overview, top):
    items = []
    for i, r in enumerate(rows[:top], 1):
        body = re.sub(r"\s+", " ", r["content"])[:500] or "（无正文）"
        body = re.sub(r"\b[0-9a-f]{16,}\b", "[图]", body)
        items.append(f"{i}. [{r['category']}] {r['title']}｜👁{r['views']} 💬{r['replies']} "
                     f"❤{r['likes']} @{r['author']} {r['time']}\n   正文：{body}")
    return (
        f"以下是 Linux.do 社区 近 {overview['days']} 天（{overview['start']} ~ {overview['end']}）的抓取数据。\n\n"
        f"【统计】新帖 {overview['posts']} 条（含正文 {overview['with_content']} 条），"
        f"上一个等长窗口 {overview.get('prev_posts', 0)} 条，"
        f"总浏览 {overview['views']}，总回复 {overview['replies']}。\n"
        f"板块分布：{overview['categories']}\n"
        f"主题分布：{[(t['name'], t['posts']) for t in overview['themes'] if t['posts']]}\n\n"
        f"【热度 Top {min(top, len(rows))} 帖子（按浏览量+回复加权排序）】\n" + "\n".join(items) +
        "\n\n请基于以上材料写一份中文日报的分析部分，严格按以下 Markdown 结构输出，"
        "不要写任何开场白、结束语，也不要写数据统计段落：\n"
        "## ⚡ 高价值信息\n（4-6 条，每条以 `- ⭐ ` 开头，格式：**［类别］一句话结论** —— 支撑细节或数字。"
        "只放真正值得单独记住的硬信息：安全事件、版本/能力发布、额度与政策变动、封号风险、"
        "重要工具或数据。按重要性排序，不要写泛泛的感想）\n\n"
        "## 🔥 热点分析\n（3-4 个热点，每个用 `### 小标题`，正文 2-4 句：先说现象，再给数据，最后给判断。"
        "必须引用具体帖子标题；要有观点，不要复述标题）\n\n"
        "要求：\n"
        "1) 只使用上面给出的数字，不要自己编造任何统计数据；\n"
        "2) 高价值信息必须是「看完就能用／能避坑」的具体信息，不要写情绪和套话；\n"
        "3) 全文 800-1200 字；\n"
        "4) 不要输出「数据概览」「热帖榜」「话题汇总」，那些由程序附加。"
    )


def call_llm(prompt, api_key, base_url, model, timeout=180):
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content":
                "你是 Linux.do 社区资深数据分析师，擅长从零散帖子里提炼主线、判断趋势、"
                "识别风险信号。语言克制、具体、不堆砌形容词。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.6,
        "max_tokens": 4000,
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip(), body.get("usage") or {}


CARD_TAG_COLORS = {
    # 只保留三种语义色，避免卡片配色过载（P3/P6）
    "安全": "red", "风险": "red", "封号": "red", "争议": "red",
    "白嫖": "green", "福利": "green", "利好": "green",
    "额度": "blue", "政策": "blue", "国产": "blue", "模型": "blue",
    "工具": "blue", "版本": "blue",
}


def parse_card_sections(markdown):
    """把报告 Markdown 按 `## ` 拆成 {标题: [行]}。"""
    sections, current = {}, None
    for line in markdown.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


def _find_section(sections, keyword):
    for title, lines in sections.items():
        if keyword in title:
            return title, lines
    return None, []


def parse_high_value(lines):
    """解析 `- ⭐ **［类别］标题**` + 缩进详情 的高价值信息条目。"""
    items, current = [], None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ⭐"):
            if current:
                items.append(current)
            raw = stripped[len("- ⭐"):].strip()
            tag = ""
            match = re.match(r"\*\*［(.+?)］(.*?)\*\*", raw)
            if match:
                tag, title = match.group(1), match.group(2).strip()
            else:
                title = raw.strip("*").strip()
            tail = raw[match.end():].strip() if match else ""
            current = {"tag": tag, "title": title, "tail": tail.lstrip("— -").strip(), "details": []}
        elif current is not None and stripped:
            current["details"].append(stripped.lstrip("- ").strip())
    if current:
        items.append(current)
    return items


def parse_subsections(lines):
    """解析 `### 小标题` + 正文。"""
    out, current = [], None
    for line in lines:
        if line.startswith("### "):
            if current:
                out.append(current)
            current = {"title": line[4:].strip(), "body": []}
        elif current is not None and line.strip():
            current["body"].append(line.strip())
    if current:
        out.append(current)
    return out


def _panel(title, children, expanded=False, emphasized=False):
    panel = {
        "tag": "collapsible_panel",
        "expanded": expanded,
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "width": "fill",
        },
        "elements": children or [{"tag": "markdown", "content": "（无）"}],
    }
    if emphasized:
        panel["background_color"] = "blue-50"
        panel["border"] = {"color": "blue-100", "corner_radius": "8px"}
        panel["padding"] = "8px"
    return panel


def _window_subtitle(markdown, day, days):
    """卡片副标题：优先取报告里的 `> ...` 窗口行。"""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            return stripped[1:].strip()
    return f"近 {days} 天（截止 {day}）"


def build_card(markdown, headline, subtitle=""):
    """把日报 Markdown 转成飞书 Card 2.0 互动卡片。"""
    sections = parse_card_sections(markdown)

    elements = []

    _, hv_lines = _find_section(sections, "高价值信息")
    items = parse_high_value(hv_lines)
    if items:
        children = []
        for item in items:
            color = CARD_TAG_COLORS.get(item["tag"], "neutral")
            lines = []
            title = f"**{item['title']}**" + (f" <text_tag color='{color}'>{item['tag']}</text_tag>" if item["tag"] else "")
            if item["tail"]:
                lines.append(item["tail"])
            lines.extend(item["details"])
            children.append({"tag": "markdown", "content": title + "\n" + "\n".join(lines)})
        elements.append(_panel(f"⚡ 高价值信息 · {len(items)} 条", children,
                               expanded=True, emphasized=True))

    _, hot_lines = _find_section(sections, "热点分析")
    subs = parse_subsections(hot_lines)
    if subs:
        children = [{"tag": "markdown", "content": f"**{s['title']}**\n" + "\n".join(s["body"])} for s in subs]
        elements.append(_panel(f"🔥 热点分析 · {len(subs)} 条", children, expanded=True))

    _, topic_lines = _find_section(sections, "话题汇总")
    if topic_lines:
        body = "\n".join(topic_lines).strip()
        if body:
            elements.append(_panel("📌 话题汇总", [{"tag": "markdown", "content": body}]))

    _, more_lines = _find_section(sections, "其它高热帖")
    if more_lines:
        body = "\n".join(more_lines).strip()
        if body:
            elements.append(_panel("🔎 其它高热帖", [{"tag": "markdown", "content": body}]))

    elements.append({"tag": "markdown", "text_size": "notation",
                     "content": "<font color='grey'>由 Linuxdoday 抓取管线自动生成</font>"})

    header = {"title": {"tag": "plain_text", "content": headline}, "template": "blue"}
    if subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": subtitle}

    return {
        "schema": "2.0",
        "config": {
            "width_mode": "default",
            "summary": {"content": headline},
        },
        "header": header,
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 16px 12px",
            "vertical_spacing": "8px",
            "elements": elements,
        },
    }


def push_card(chat_id, card, identity="auto"):
    import subprocess
    cmd = ["lark-cli", "--profile", os.environ.get("LARK_PROFILE", "claw"),
           "im", "+messages-send", "--chat-id", chat_id,
           "--msg-type", "interactive", "--content", json.dumps(card, ensure_ascii=False)]
    if identity and identity != "auto":
        cmd += ["--as", identity]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"飞书卡片推送失败: {(result.stderr or result.stdout)[:400]}")
    payload = json.loads(result.stdout or "{}")
    if not payload.get("ok", True):
        raise RuntimeError(f"飞书卡片推送失败: {json.dumps(payload.get('error'), ensure_ascii=False)[:400]}")


def push_chat(chat_id, text, title, identity="auto"):
    import subprocess
    cmd = ["lark-cli", "--profile", os.environ.get("LARK_PROFILE", "claw"),
           "im", "+messages-send", "--chat-id", chat_id, "--markdown", text]
    if identity and identity != "auto":
        cmd += ["--as", identity]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"飞书推送失败: {(result.stderr or result.stdout)[:300]}")
    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError:
        return
    if not payload.get("ok", True):
        raise RuntimeError(f"飞书推送失败: {json.dumps(payload.get('error'), ensure_ascii=False)[:300]}")


def to_message(markdown, headline=""):
    """把 Markdown 日报转成飞书消息友好格式。

    飞书 post 消息对 Markdown 表格支持差，这里把表格转成列表；
    一级标题去掉后由调用方用加粗行代替。
    """
    out, table = [], []

    def flush_table():
        if not table:
            return
        header = [c.strip() for c in table[0].strip('|').split('|')]
        for row in table[2:]:
            cells = [c.strip() for c in row.strip('|').split('|')]
            if not any(cells):
                continue
            link = None
            for cell in cells:
                match = re.match(r'\[(.+?)\]\((\S+?)\)', cell)
                if match:
                    link = match
                    break
            if link and cells and cells[0].isdigit():
                rest = " · ".join(c for c in cells[1:] if c and c != link.group(0))
                out.append(f"{cells[0]}. [{link.group(1)}]({link.group(2)})" + (f" · {rest}" if rest else ""))
            else:
                out.append("- " + " · ".join(c for c in cells if c and c != '---'))
        table.clear()

    for line in markdown.splitlines():
        if line.lstrip().startswith('|'):
            table.append(line)
            continue
        flush_table()
        if line.startswith('# ') or line.startswith('---'):
            continue
        if line.startswith('## '):
            line = "**" + line[3:].strip() + "**"
        elif line.startswith('### '):
            line = "▪ " + line[4:].strip()
        out.append(line)
    flush_table()
    return re.sub(r'\n{3,}', '\n\n', "\n".join(out)).strip()


def main():
    ap = argparse.ArgumentParser(description="生成 Linux.do AI 日报")
    ap.add_argument("--date", default="", help="窗口截止日期 YYYY-MM-DD（默认今天，CST）")
    ap.add_argument("--days", type=int, default=int(os.environ.get("DAILY_REPORT_DAYS", "3")),
                    help="数据窗口天数（默认 3，即近 3 天）")
    ap.add_argument("--top", type=int, default=30, help="送入模型的热帖条数")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--no-llm", action="store_true", help="不调用模型，只输出统计部分")
    ap.add_argument("--push-chat", default="", help="生成后推送到该飞书会话 chat_id")
    ap.add_argument("--push-as", default=os.environ.get("DAILY_REPORT_PUSH_AS", "auto"),
                    choices=["auto", "user", "bot"], help="推送身份（bot 私聊用 bot）")
    ap.add_argument("--push-only", action="store_true",
                    help="不重新生成，直接把已有日报文件推送到飞书")
    ap.add_argument("--card", action="store_true",
                    help="以飞书互动卡片（Card 2.0）推送，而不是 Markdown 消息")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写文件/不推送")
    args = ap.parse_args()

    day = (datetime.fromisoformat(args.date).date() if args.date
           else datetime.now(TZ).date())

    if args.push_only:
        path = os.path.join(REPORT_DIR, f"AI日报-{day}.md")
        if not os.path.exists(path):
            log(f"⚠️ 日报文件不存在: {path}")
            return 1
        with open(path, "r", encoding="utf-8") as f:
            markdown = f.read()
        label = f"（近 {args.days} 天）" if args.days > 1 else ""
        headline = f"Linux.do AI 日报 · {day}{label}"
        subtitle = _window_subtitle(markdown, day, args.days)
        message = f"📊 **{headline}**\n\n" + to_message(markdown)
        if args.dry_run or not args.push_chat:
            print(json.dumps(build_card(markdown, headline, subtitle), ensure_ascii=False, indent=1)
                  if args.card else message)
            return 0
        try:
            if args.card:
                push_card(args.push_chat, build_card(markdown, headline, subtitle), args.push_as)
            else:
                push_chat(args.push_chat, message, headline, args.push_as)
            log(f"已推送 {path} → 飞书 {args.push_chat}（{args.push_as}{'，卡片' if args.card else ''}）")
        except Exception as exc:
            log(f"⚠️ {exc}")
            return 2
        return 0

    rows, overview, hot = collect(day, args.days)
    if not rows:
        log(f"{day} 前 {args.days} 天没有数据，跳过")
        return 1
    log(f"{overview['start']} ~ {overview['end']}：{len(rows)} 帖（含正文 {overview['with_content']} 条）")

    narrative, usage = "", {}
    if args.no_llm:
        narrative = "## ⚡ 高价值信息\n\n（本次未启用模型分析，下面是自动生成的话题汇总与热帖）\n"
    else:
        api_key = read_api_key()
        if not api_key:
            log("⚠️ 未找到 API key（DAILY_REPORT_API_KEY 或 ~/.dsh/.credentials.yaml），降级为统计版")
            args.no_llm = True
            narrative = "## ⚡ 高价值信息\n\n（未找到模型凭据，下面是自动生成的话题汇总与热帖）\n"
        else:
            try:
                narrative, usage = call_llm(build_prompt(rows, overview, args.top),
                                            api_key, args.base_url, args.model)
                log(f"模型 {args.model} 返回 {len(narrative)} 字，用量 {usage.get('total_tokens', '?')} tokens")
            except (urllib.error.URLError, KeyError, ValueError) as exc:
                log(f"⚠️ 模型调用失败（{exc}），降级为统计版")
                narrative = f"## ⚡ 高价值信息\n\n（模型调用失败：{exc}）\n"

    daily_hot = (hot.get("daily") or [])
    label = f"（近 {args.days} 天）" if args.days > 1 else ""
    headline = f"Linux.do AI 日报 · {day}{label}"
    subtitle = ""
    parts = [f"# {headline}", "",
             f"> {overview['start']} ~ {overview['end']}（近 {args.days} 天）｜{len(rows)} 帖"
             f"（上一窗口 {overview.get('prev_posts', 0)}）",
             "", narrative.strip(), "", "---", "",
             render_topics(rows, overview), "", "---", "",
             render_hot(rows, hot, args.top), "",
             "*由 Linuxdoday 抓取管线自动生成：帖子/指标来自板块抓取，正文来自 `/t/<slug>/<id>.json`。*"]

    markdown = "\n".join(parts)
    if args.dry_run:
        print(markdown[:2000])
        return 0

    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"AI日报-{day}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)
    log(f"日报已写入 {path}")

    if args.push_chat:
        try:
            if args.card:
                push_card(args.push_chat, build_card(markdown, headline, subtitle), args.push_as)
            else:
                message = f"📊 **{headline}**\n\n" + to_message(markdown)
                push_chat(args.push_chat, message, headline, args.push_as)
            log(f"已推送到飞书 {args.push_chat}（{args.push_as}{'，卡片' if args.card else ''}）")
        except Exception as exc:
            log(f"⚠️ {exc}")
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
