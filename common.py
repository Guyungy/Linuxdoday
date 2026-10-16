#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""common.py — 抓取 → 回填 → 推送 → 日报 这条流水线共用的底层零件。

这些东西原先在每个脚本里各有一份副本，而且注释自己写着「两处口径必须一致」
（normalize_datetime 就是这么写的）—— 但没有任何机制保证。副本漂移过一次：
DOM 回退把标签文本写进时间字段，认证出脏值，一次废掉 937 条飞书记录
（2026-10-01）。这个模块就是那个「机制」：口径只有一处，改一处即全局生效。

几条硬约束，动之前先看：

1. **零第三方依赖。** 这些脚本被 service.py 以子进程方式拉起，镜像里只装了
   requirements-service.txt / requirements-browser.txt。这里只能用标准库。

2. **每个用法都必须是子进程脚本能直接 import 的。** Python 会把脚本所在目录
   放进 sys.path[0]，所以 ROOT 下的 common.py 对同目录脚本始终可见，不需要
   任何安装步骤 —— 但也就意味着新增文件必须进 Dockerfile.service 的 COPY
   （tests/test_core.py 的静态闭包断言会拦）。

3. **这里只放「多处共用且必须一致」的东西。** 只有一个调用方的东西留在原地：
   过早抽象比重复更难改。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
# 代理默认值。原来在 browser_utils / hot_topics / fetch_content /
# linux_do_scraper / fetch_replies 五个文件里各写了一遍完全相同的表达式。
PROXY_DEFAULT = os.environ.get("LINUXDO_PROXY", "")

# ---------------------------------------------------------------------------
# 跨进程降级通道（WARN 协议）
# ---------------------------------------------------------------------------
# 约定：子进程把「本轮有降级、但没到失败」的现场写成 stderr 上带此前缀的行。
# 为什么需要它：service.py 只在 returncode != 0 时才留 stderr，成功分支会把
# stderr 整段丢掉 —— 于是热榜抓失败、正文回填失败这类「降级但没失败」的证据
# 采集了却查不到。带前缀的行会被 extract_warnings() 捞出来存进 latest_run.json。
#
# 这是 service.py 与子进程之间**唯一**的结构化通道，所以两侧共用同一份定义：
# 前缀改了却只改一边 = 降级现场静默消失。
WARN_PREFIX = "WARN: "


def warn(msg):
    """写一条降级现场到 stderr（带时间戳与前缀）。"""
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {WARN_PREFIX}{msg}", file=sys.stderr, flush=True)


def extract_warnings(stderr):
    """从子进程 stderr 里捞出 WARN: 行 —— 成功轮次里唯一能带出降级现场的通道。"""
    found = []
    for line in (stderr or "").splitlines():
        marker = line.find(WARN_PREFIX)
        if marker == -1:
            continue
        message = line[marker + len(WARN_PREFIX):].strip()
        if message:
            found.append(message)
    return found


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def log(msg, tz=None):
    """一行带时间戳的日志，写 stderr（stdout 留给 JSON 产物，别污染管道）。

    tz=None 用机器本地时区；日报要显式传 TZ（UTC+8），因为日报的窗口、
    文件名、正文日期全是 UTC+8 口径，日志跟着本地时区会在容器（UTC）里
    和报告对不上。
    """
    now = datetime.now(tz) if tz is not None else datetime.now()
    print(f"[{now.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# 文件读写
# ---------------------------------------------------------------------------
def load_json(path, default):
    """读 JSON；文件不存在 / 读不动 / 不是合法 JSON 都返回 default。

    只接 (OSError, ValueError)：JSON 坏掉是 ValueError 的子类 JSONDecodeError，
    文件问题走 OSError。不接裸 Exception —— 那会把真正该暴露的 bug
    （比如传错了 path 类型）也一起吞掉。
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json_atomic(path, value, indent=1):
    """原子写 JSON：先写同目录临时文件，再 os.replace 换上去。

    直接 open(path, "w") 会**先**把原文件截断成 0 字节再写，定时任务与读者
    并发时对方就读到半截 JSON。os.replace 在同一文件系统内是原子的：读者
    要么看到旧完整版，要么看到新完整版。
    """
    path = str(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=indent)
    os.replace(temporary, path)


def write_text_atomic(path, text):
    """原子写文本（日报用）。理由同 write_json_atomic。"""
    path = str(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temporary, path)


# ---------------------------------------------------------------------------
# 时间归一化
# ---------------------------------------------------------------------------
# 飞书 datetime 字段只吃 RFC3339 或 "YYYY-MM-DD HH:MM:SS"，喂别的直接 800010403
# invalid_request；而 record-batch-create 是整批原子的 —— 一条脏值废掉整批 200 条
# （2026-10-01 那次 937 条全灭就是被 29 条脏时间拖的：DOM 回退把标签文本写进了
# 时间字段）。所以所有时间在**出站前**统一过这道闸，认不出来就返回空串
# （飞书侧即留空），绝不把原始值透传出去。
_DT_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
_DT_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DT_CN_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\D*(\d{1,2})?\s*[:：]?\s*(\d{1,2})?")
_DT_EPOCH_RE = re.compile(r"^\d{10,13}$")


def normalize_datetime(value):
    """把抓来的时间值收敛成飞书 datetime 字段能接受的字符串；认不出来返回 ""。

    接受四种输入：epoch 秒/毫秒、ISO（空格或 T 分隔，秒可省）、纯日期、
    中文「YYYY年M月D日 [H[:M]]」。其余一律空串。
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if _DT_EPOCH_RE.match(text):
        try:
            stamp = int(text)
            if stamp > 10 ** 12:
                stamp //= 1000
            return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            return ""
    m = _DT_ISO_RE.match(text)
    if m:
        year, month, day, hour, minute, second = m.groups()
        return f"{year}-{month}-{day} {hour}:{minute}:{second or '00'}"
    m = _DT_DATE_RE.match(text)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)} 00:00:00"
    m = _DT_CN_RE.search(text)
    if m:
        year, month, day = m.group(1), int(m.group(2)), int(m.group(3))
        hour, minute = int(m.group(4) or 0), int(m.group(5) or 0)
        return f"{year}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:00"
    return ""


# ---------------------------------------------------------------------------
# 采集时间
# ---------------------------------------------------------------------------
def now_iso():
    """UTC ISO8601（秒精度）。采集时间戳统一走这里。

    走本地时区会让同一批行在开发机（CST）与容器（UTC）里对不上 ——
    日报按 UTC+8 口径切窗口，时间戳却是本地时区，两边就会打架。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 行的来源（provenance）
# ---------------------------------------------------------------------------
# 缓存里混着两种行：浏览器内 fetch Discourse JSON 采的（浏览/回复/点赞齐全），
# 和 RSS feed 采的（三个指标恒为 0 —— 那是「**没测**」，不是「没人看」）。
#
# 这个区别以前只隐含在「指标是不是 0」上，于是日报把 13,960 条从没测过的行
# 按 0 热度排进「长尾(<100)」，还告诉模型「这是按浏览量+回复加权排序的 Top N」
# —— 排序键对这些行恒为 0，所谓 Top 只是缓存顺序，提示词在骗自己。
# 指标为 0 也不等于没测过：浏览器采到一条刚发的新帖，指标**确实**就是 0
# （实测缓存里有这么一行）。所以必须显式记来源，不能靠 0 反推。
SOURCE_BROWSER = "browser"   # 浏览器内 fetch Discourse JSON：指标齐全
SOURCE_DOM = "dom"           # 浏览器 DOM 回退：有浏览/回复，但没有点赞/海报名/原生标签
SOURCE_RSS = "rss"           # RSS feed：无指标，字段也少
SOURCE_HOT = "hot"           # 官方热榜 /top.json：有指标
VALID_SOURCES = (SOURCE_BROWSER, SOURCE_DOM, SOURCE_RSS, SOURCE_HOT)

# 每种来源「看不全」的字段：merge 时这些字段**不**用该来源的新值覆盖旧值。
# 判据只有一个 —— 拿信息量更低的观测去盖掉信息量更高的观测，就是丢数据。
# 这张表是 linux_do_scraper.merge_incremental 里那串硬编码键名的来历。
UNOBSERVED_FIELDS = {
    # RSS 恒为 0 / 不提供 / 恒为空，或由 pubDate 重算（格式与精度都比 JSON 差）。
    SOURCE_RSS: frozenset({
        "replies", "views", "like_count", "posts_count",
        "bumped_at", "slug", "tags_raw", "created_at",
    }),
    # 列表页 DOM 上没有点赞数与原生标签栏；时间只有 UTC+8 墙钟文本。
    # 注意 DOM 行**根本不带** like_count / tags_raw / slug 这几个键（列表页 JS
    # 只 push 了 id/title/url/replies/views/author/created_at/bumped_at），
    # 所以那三项在这里是留白而非零值 —— 一旦哪天 JS 补上它们，这行就该挡住。
    # 真正会命中这一条的是 created_at：DOM 的时间格式与精度都比 discourse JSON 差。
    SOURCE_DOM: frozenset({"like_count", "tags_raw", "slug", "created_at"}),
}


def has_metrics(row):
    """行里是否带真实互动指标（浏览 / 回复 / 点赞任一非 0）。"""
    return bool(int(row.get("views") or 0) or int(row.get("replies") or 0)
                or int(row.get("like_count") or 0))


def source_of(row):
    """行的来源。显式 source 字段优先；老数据按形状回推。

    回推规则取三条信号的**并集**（任一命中即 browser），按可靠性排序：
      - 有互动指标 → 只有浏览器/热榜拿得到；
      - 有 posters / category_id / last_poster → RSS 行从不写这三个键；
      - created_at 以 "Z" 结尾 → 原始 Discourse JSON 才是这个格式，RSS 走
        parsedate_to_datetime().isoformat() 得到的是 "+00:00"。
    三条全不中才算 RSS。实测 37,729 行老数据上三者互相印证。

    回推**分不出** browser 与 dom（老数据里两者都只表现为「有指标」），
    所以老数据统一记 browser。这是老数据能支撑的粒度 —— 不硬猜。
    """
    explicit = row.get("source")
    if explicit in VALID_SOURCES:
        return explicit
    if has_metrics(row):
        return SOURCE_BROWSER
    if "posters" in row or "category_id" in row or "last_poster" in row:
        return SOURCE_BROWSER
    if (row.get("created_at") or "").endswith("Z"):
        return SOURCE_BROWSER
    return SOURCE_RSS


def is_measured(row):
    """这行的指标是否「测过」。日报的排序 / 分档 / 提示词都按它区分。

    没测过 ≠ 热度低：13,960 条 RSS 行的 👁0 💬0 是采集口径的产物，
    把它们混进「长尾帖」统计等于凭空断言它们没人看。
    """
    return source_of(row) != SOURCE_RSS


# ---------------------------------------------------------------------------
# 帖子行（topic row）
# ---------------------------------------------------------------------------
# 缓存里的每一行都必须是这个形状。两个来源各写了一遍这个 dict 字面量
# （linux_do_scraper 从板块 JSON、hot_topics 从 /top.json），副本漂移过一次：
# posts_count 的回退口径不同 —— 抓取端是 `posts_count or reply_count`，
# 热榜端是 `posts_count or 0`。同一个帖子从两个入口进来会得到不同的行。
#
# 作者与海报名两边的取法**确实**不同（抓取端用 user_map 把 user_id 解成
# username；热榜端的 posters 直接带 username），所以那两项由调用方算好传进来，
# 不在这里猜。其余字段统一从这里出。
TOPIC_FIELDS = (
    "id", "title", "url", "replies", "views", "author", "created_at", "bumped_at",
    "slug", "like_count", "posts_count", "posters", "last_poster", "category_id",
    "pinned", "visible", "tags_raw",
    # 来源与「这批指标是什么时候看的」。RSS 行不走本函数，由 linux_do_scraper
    # 那一处显式写上（它连 posters 都没有，形状本就不同 —— 见该处注释）。
    "source", "metrics_at",
)


def topic_row(raw, author="", posters=None, last_poster=None, category=None,
              category_id=None, source=None, metrics_at=None):
    """把一条 Discourse JSON 帖子转成缓存行（TOPIC_FIELDS 的公共子集）。

    raw 是原始 JSON dict；author / posters / last_poster 是调用方按各自来源解析
    好的作者信息 —— 两个来源的解析口径确实不同（一个能查 user_id→username 反查表，
    一个拿不到），所以在这里猜只会把差异藏起来，不如显式传。

    category 用 None 而不是 "" 作默认：调用方**没传**该参数时行里不带 "category"
    键（抓取端稍后再按 category_id 反查填名），传了就一定带上这个键（哪怕值是
    空串）。两者都是既有行为，不能合并。

    source / metrics_at 同理用 None 作默认：没传就不带键。RSS 那条路径压根不走
    本函数，它手写行字面量并显式标 source=SOURCE_RSS（理由见 linux_do_scraper）。
    """
    posters = list(posters or [])
    tid = str(raw.get("id", ""))
    slug = raw.get("slug") or ""
    if category_id is None:
        category_id = raw.get("category_id") or ""
    if last_poster is None:
        last_poster = posters[-1] if posters else ""
    row = {
        "id": tid,
        "title": (raw.get("title") or "")[:200],
        "url": f"/t/{slug}/{tid}" if slug else f"/t/topic/{tid}",
        "replies": int(raw.get("reply_count") or 0),
        "views": int(raw.get("views") or 0),
        "author": author or "",
        "created_at": raw.get("created_at") or "",
        "bumped_at": raw.get("bumped_at") or "",
        "slug": slug,
        "like_count": int(raw.get("like_count") or 0),
        # 回退到 reply_count：Discourse 的 posts_count 缺失时，回复数是最接近的
        # 可用替代（原帖占 1）。两个入口必须给同一个答案。
        "posts_count": int(raw.get("posts_count") or raw.get("reply_count") or 0),
        "posters": posters,
        "last_poster": last_poster,
        "category_id": category_id,
        "pinned": bool(raw.get("pinned")),
        "visible": bool(raw.get("visible")),
        "tags_raw": [x for x in (raw.get("tags") or []) if isinstance(x, str)],
    }
    if source is not None:
        row["source"] = source
    if metrics_at is not None:
        row["metrics_at"] = metrics_at
    if category is not None:
        row["category"] = category
    return row
