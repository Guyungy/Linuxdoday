#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_to_feishu.py — 把抓取的帖子数据写入飞书多维表格（增量）

用法：
  # 直接把 linux_do_scraper.py 的输出管道进来（增量，只写新帖）
  python linux_do_scraper.py --scrape | python push_to_feishu.py

  # 或从缓存文件推全量（会把全部记录都尝试写，Topic ID 去重）
  python push_to_feishu.py --file data/linuxdo_topics.json

  参数:
    --base-token  多维表格 token（默认 LYdZbR3DTaFPeYsHP8ScqPVCnFe）
    --table       表名（默认 帖子主题）
    --file        从本地 JSON 文件读取（而非 stdin）
    --dry-run     只打印将写入的条数，不真正写飞书
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_BASE_TOKEN = "LYdZbR3DTaFPeYsHP8ScqPVCnFe"
DEFAULT_TABLE = "帖子主题"
BATCH = 200  # lark-cli 单次最大 200

# 飞书 datetime 字段只吃 RFC3339 或 "YYYY-MM-DD HH:MM:SS"，喂别的直接 800010403
# invalid_request；而 record-batch-create 是整批原子的 —— 一条脏值废掉整批 200 条
# （2026-10-01 那次 937 条全灭就是被 29 条脏时间拖的：DOM 回退把标签文本写进了时间字段）。
# 所以所有时间在写入前统一过这道闸，认不出来就返回空串（飞书侧即留空）。
# 见 linux_do_scraper.py 的同名函数，两处口径必须一致。
_DT_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
_DT_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DT_CN_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\D*(\d{1,2})?\s*[:：]?\s*(\d{1,2})?")
DATETIME_FIELDS = ("发布时间", "最近活跃")


def normalize_datetime(value):
    """把时间值收敛成飞书 datetime 字段能接受的字符串；认不出来返回 ""。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if re.match(r"^\d{10,13}$", text):
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


def sanitize_rows(rows):
    """写入前的字段闸门：时间字段归一化 + 链接取纯文本 + 标签补空。

    返回 (rows, 修正条数)。已格式化的行（如 pending_feishu.json，带"标题"键）
    不会经过 feishu_rows()，所以这道闸必须在两条入口的公共路径上都跑。
    """
    out, patched = [], 0
    for row in rows:
        r = dict(row)
        for field in DATETIME_FIELDS:
            if field not in r:
                continue
            fixed = normalize_datetime(r.get(field))
            if fixed != r.get(field):
                patched += 1
                r[field] = fixed
        link = r.get("帖子链接")
        if isinstance(link, dict):
            r["帖子链接"] = link.get("link") or link.get("text") or ""
        r.setdefault("标签", [])
        out.append(r)
    return out, patched

# 关键：必须用创建该 Base 的 app profile（claw / cli_aa0112b836bf5be2）。
# 默认 active profile 是 huidu（cli_aa20b02703b99d18），对这张表无权限（91403）。
# 命名 profile 见 ~/.lark-cli/config.json（config init --name claw）。
LARK_PROFILE = os.environ.get("LARK_PROFILE", "claw")
CACHE_FILE = Path(__file__).resolve().parent / "data" / "feishu_topic_ids.json"
REJECT_FILE = Path(__file__).resolve().parent / "data" / "rejected_topics.json"
CONTENT_FILE = Path(__file__).resolve().parent / "data" / "topic_content.json"
CACHE_TTL = max(0, int(os.environ.get("FEISHU_ID_CACHE_SECONDS", "86400")))
MAX_CONTENT_CHARS = 50000  # 飞书文本字段上限 10 万字符，留足余量


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def load_id_cache():
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        age = time.time() - float(payload.get("updated_at", 0))
        if age <= CACHE_TTL:
            return {str(value) for value in payload.get("topic_ids", [])}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return None


def save_id_cache(ids):
    atomic_write_json(CACHE_FILE, {
        "updated_at": time.time(),
        "topic_ids": sorted(ids),
    })


def feishu_rows(rows):
    """topic 原始记录 → 飞书字段格式"""
    out = []
    for r in rows:
        out.append({
            "标题": r.get("title", ""),
            "帖子链接": "https://linux.do" + (r.get("url") or ""),
            "作者": r.get("author", ""),
            "板块": r.get("category", ""),
            "回复数": int(r.get("replies", 0) or 0),
            "浏览量": int(r.get("views", 0) or 0),
            "发布时间": normalize_datetime(r.get("created_at")),
            "最近活跃": normalize_datetime(r.get("bumped_at")),
            "Topic ID": r.get("id", ""),
            "标签": r.get("tags") or [],
            "正文": r.get("content", ""),
        })
    return out


def load_from_stdin():
    """读 stdin 的 JSON。上游抓取失败时管道会传来空内容，这里给出可读提示
    而不是抛 JSONDecodeError 堆栈。"""
    raw = sys.stdin.read()
    if not raw.strip():
        print("✗ 上游没有输出任何数据（抓取可能失败）。"
              "请检查 linux_do_scraper.py 的 stderr 日志。", file=sys.stderr)
        sys.exit(2)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"✗ 上游输出不是合法 JSON：{exc}\n  前 200 字符: {raw[:200]!r}", file=sys.stderr)
        sys.exit(2)


def load_from_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def existing_topic_ids(base_token, table):
    """查询飞书表中已存在的 Topic ID 集合（幂等去重用）"""
    cached = load_id_cache()
    if cached is not None:
        print(f"  已从本地索引读取 {len(cached)} 个 Topic ID")
        return cached
    ids = set()
    offset = 0
    while True:
        cmd = [
            "lark-cli", "--profile", LARK_PROFILE, "base", "+record-list",
            "--base-token", base_token,
            "--table-id", table,
            "--field-id", "Topic ID",
            "--offset", str(offset),
            "--limit", "200",
            "--format", "json",
            "--as", "user",
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                print(f"  ⚠️ 查询已有 Topic ID 失败: {r.stderr[:300]}", file=sys.stderr)
                return None
            d = json.loads(r.stdout)
            rows = (d.get("data") or {}).get("data") or []
            for row in rows:
                # json 矩阵：每行 [[value]]（单字段投影）或 [["a","b"]]（多字段）
                cell = row[0] if row else None
                if isinstance(cell, list):
                    cell = cell[0] if cell else None
                if cell is not None:
                    ids.add(str(cell))
        except Exception as e:
            print(f"  ⚠️ 查询已有 Topic ID 异常: {e}", file=sys.stderr)
            return None
        if len(rows) < 200:
            break
        offset += len(rows)
    save_id_cache(ids)
    return ids


def dedupe_against_feishu(base_token, table, rows):
    """按 Topic ID 剔除飞书表中已存在的行（幂等双保险）"""
    ids = existing_topic_ids(base_token, table)
    if ids is None:
        return rows, 0
    kept = [r for r in rows if str(r.get("Topic ID", "")) not in ids]
    skipped = len(rows) - len(kept)
    return kept, skipped


def payload_records(chunk):
    """空时间字段整键剔除。

    飞书 datetime 字段收到空串同样报 800010403（只认 RFC3339 / 本地时间字面量），
    只有「键不存在」才等于留空。
    """
    return [
        {k: v for k, v in row.items() if not (k in DATETIME_FIELDS and v in ("", None))}
        for row in chunk
    ]


def create_records(base_token, table, chunk, dry_run=False):
    """提交一批（≤BATCH 条）。返回 (created, error)。"""
    payload = json.dumps({"create_records": payload_records(chunk)}, ensure_ascii=False)
    cmd = [
        "lark-cli", "--profile", LARK_PROFILE, "base", "+record-batch-create",
        "--base-token", base_token,
        "--table-id", table,
        "--json", payload,
    ]
    if dry_run:
        cmd.append("--dry-run")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        return 0, (r.stderr or r.stdout or "unknown error")[:400]
    try:
        res = json.loads(r.stdout)
        # lark-cli +record-batch-create 成功返回：data 可能是 dict（含 records/record_id_list）或 list
        data = res.get("data")
        if isinstance(data, dict):
            return len(data.get("records") or data.get("record_id_list") or []), None
        if isinstance(data, list):
            return len(data), None
    except Exception:
        pass
    return len(chunk), None


def push_chunk(base_token, table, chunk, dry_run=False, known_ids=None, rejects=None):
    """写一批；整批被拒就二分劈开重试。

    record-batch-create 是整批原子的：一条脏数据（比如时间字段喂了标签文本）
    会让同批 200 条一起 400。旧行为是 break —— 直接放弃后续所有批
    （2026-10-01 12:16：937 条全灭，一条没进）。现在劈到单条，坏行记进 rejects，
    好行照写，最坏也只丢真正有问题的那几条。
    """
    if rejects is None:
        rejects = []
    if not chunk:
        return 0
    created, error = create_records(base_token, table, chunk, dry_run=dry_run)
    if error is None:
        if known_ids is not None and created == len(chunk):
            known_ids.update(str(row.get("Topic ID")) for row in chunk if row.get("Topic ID"))
            save_id_cache(known_ids)
        return created
    if len(chunk) == 1:
        rejects.append({
            "Topic ID": chunk[0].get("Topic ID"),
            "标题": chunk[0].get("标题"),
            "error": error,
        })
        print(f"  ⚠️ 跳过 1 条（Topic ID {chunk[0].get('Topic ID')}）：{error[:200]}", file=sys.stderr)
        return 0
    mid = len(chunk) // 2
    print(f"  ⚠️ {len(chunk)} 条被整批拒（{error[:120]}），二分重试", file=sys.stderr)
    return (push_chunk(base_token, table, chunk[:mid], dry_run, known_ids, rejects)
            + push_chunk(base_token, table, chunk[mid:], dry_run, known_ids, rejects))


def push(base_token, table, rows, dry_run=False, known_ids=None):
    """返回 (written, rejects)。rejects = 被飞书拒绝、已隔离的坏行。"""
    total = len(rows)
    if total == 0:
        print("无待写入记录")
        return 0, []
    print(f"共 {total} 条，分 { (total + BATCH - 1)//BATCH } 批（每批 ≤{BATCH}）")
    written = 0
    rejects = []
    for i in range(0, total, BATCH):
        chunk = rows[i:i + BATCH]
        print(f"  写入第 {i//BATCH+1} 批（{len(chunk)} 条）...", flush=True)
        created = push_chunk(base_token, table, chunk, dry_run=dry_run,
                             known_ids=known_ids, rejects=rejects)
        written += created
        print(f"  ✅ 已写 {created} 条")
    if rejects:
        print(f"⚠️ {len(rejects)} 条被飞书拒绝（已隔离，其余照写）：", file=sys.stderr)
        for item in rejects[:10]:
            print(f"    Topic ID {item['Topic ID']}: {item['error'][:160]}", file=sys.stderr)
    print(f"完成：写入 {written}/{total} 条 → 表 [{table}]")
    return written, rejects


def topic_record_map(base_token, table, limit=2000):
    """返回 {Topic ID: (record_id, 已有正文?)}；用 ndjson 产物读取，避免解析 markdown 矩阵。"""
    mapping = {}
    offset = 0
    while True:
        cmd = [
            "lark-cli", "--profile", LARK_PROFILE, "base", "+record-list",
            "--base-token", base_token,
            "--table-id", table,
            "--field-id", "Topic ID",
            "--field-id", "正文",
            "--format", "ndjson",
            "--limit", str(limit),
            "--offset", str(offset),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"读取表记录失败: {(r.stderr or r.stdout)[:300]}")
        manifest = json.loads(r.stdout)
        count = 0
        record_file = manifest.get("record_file")
        if record_file:
            with open(record_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    count += 1
                    tid, rid = rec.get("Topic ID"), rec.get("record_id")
                    if tid and rid:
                        mapping[str(tid)] = (rid, bool(rec.get("正文")))
        if not manifest.get("has_more"):
            break
        offset = manifest.get("next_offset", offset + max(count, 1))
    return mapping


def sync_content(base_token, table, contents, dry_run=False):
    """把本地正文更新到表中已存在的记录；已有正文的行跳过（幂等、可重复跑）。"""
    mapping = topic_record_map(base_token, table)
    print(f"  表中记录 {len(mapping)} 条")
    updates, missing, filled = {}, 0, 0
    for tid, payload in contents.items():
        entry = mapping.get(str(tid))
        if not entry:
            missing += 1
            continue
        record_id, has_content = entry
        if has_content:
            filled += 1
            continue
        text = ((payload or {}).get("content") or "").strip()
        if not text:
            continue
        updates[record_id] = {"正文": text[:MAX_CONTENT_CHARS]}
    print(f"  待更新 {len(updates)} 条（表中无此帖 {missing}，已有正文跳过 {filled}）")
    if dry_run or not updates:
        return 0

    payload_path = Path(__file__).resolve().parent / "data" / ".content_update.json"
    items = list(updates.items())
    done = 0
    for i in range(0, len(items), BATCH):
        chunk = dict(items[i:i + BATCH])
        payload_path.write_text(json.dumps({"update_records": chunk}, ensure_ascii=False), encoding="utf-8")
        cmd = [
            "lark-cli", "--profile", LARK_PROFILE, "base", "+record-batch-update",
            "--base-token", base_token,
            "--table-id", table,
            "--json", f"@data/{payload_path.name}",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           cwd=str(Path(__file__).resolve().parent))
        if r.returncode != 0:
            print(f"  ❌ 第 {i//BATCH+1} 批更新失败: {(r.stderr or r.stdout)[:400]}", file=sys.stderr)
            break
        done += len(chunk)
        print(f"  ✅ 已更新正文 {done}/{len(items)} 条")
    payload_path.unlink(missing_ok=True)
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    ap.add_argument("--table", default=DEFAULT_TABLE)
    ap.add_argument("--file", default="", help="从本地 JSON 文件读取")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sync-content", action="store_true",
                    help="把 data/topic_content.json 的正文更新到表中已有记录（不新建行）")
    ap.add_argument("--result-file", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.sync_content:
        contents = load_from_file(str(CONTENT_FILE)) if CONTENT_FILE.exists() else {}
        print(f"本地正文 {len(contents)} 条 → 同步到 [{args.table}]")
        updated = sync_content(args.base_token, args.table, contents, dry_run=args.dry_run)
        if args.result_file:
            atomic_write_json(Path(args.result_file), {"content_updated": updated})
        return

    if args.file:
        raw = load_from_file(args.file)
    else:
        raw = load_from_stdin()

    # 支持两种输入：feishu_rows 已格式化的，或原始 topic 记录
    if raw and isinstance(raw[0], dict) and "标题" in raw[0]:
        rows = raw
    else:
        rows = feishu_rows(raw)

    # 字段闸门：时间归一化 + 链接取纯文本 + 标签补空。
    # 两条入口（已格式化行 / 原始 topic）都要过，否则 pending_feishu.json 里
    # 的历史脏时间（标签文本混进时间字段）会原样打到飞书 → 整批 400。
    rows, patched = sanitize_rows(rows)
    if patched:
        print(f"⚠️ 修正 {patched} 处非法时间字段（原值无法解析，已置空/归一）")

    if args.dry_run:
        print(f"[dry-run] 将写入 {len(rows)} 条到 [{args.table}]")
        if rows:
            print("样例:", json.dumps(rows[0], ensure_ascii=False)[:300])
        return

    # 幂等双保险：剔除飞书表中已存在的 Topic ID（防止本地缓存丢失/误删导致重复入库）
    known_ids = existing_topic_ids(args.base_token, args.table)
    if known_ids is None:
        known_ids = set()
        print("⚠️ 无法确认远端去重状态，本次停止写入以避免重复数据", file=sys.stderr)
        raise SystemExit(3)
    original = len(rows)
    rows = [r for r in rows if str(r.get("Topic ID", "")) not in known_ids]
    skipped = original - len(rows)
    if skipped:
        print(f"已跳过 {skipped} 条已入库记录（按 Topic ID 去重）")
    if not rows:
        print("无新增记录")
        if args.result_file:
            atomic_write_json(Path(args.result_file), {"requested": original, "skipped": skipped, "written": 0})
        return

    written, rejects = push(args.base_token, args.table, rows, known_ids=known_ids)
    if args.result_file:
        atomic_write_json(Path(args.result_file), {
            "requested": original, "skipped": skipped,
            "written": written, "rejected": len(rejects),
        })
    if rejects:
        # 坏行单独留证：确认是数据问题就用它复盘，修好后可重推。
        atomic_write_json(REJECT_FILE, {
            "updated_at": time.time(),
            "batch_size": BATCH,
            "rejected": rejects,
        })
        print(f"已隔离到 {REJECT_FILE.name}（{len(rejects)} 条），修好数据后可重推")
    # 一条都没进去 = 系统性失败（令牌失效/网络/表结构变更），保留 pending 下轮重试；
    # 个别坏行被隔离则算成功，不能让它们把整轮拖成"永远没写进去"。
    if written == 0 and len(rows) > 0:
        raise SystemExit(4)
    if len(rejects) > max(5, int(len(rows) * 0.2)):
        print("⚠️ 被拒比例过高（>20%），按系统性故障处理，保留 pending 待重试", file=sys.stderr)
        raise SystemExit(4)


if __name__ == "__main__":
    main()
