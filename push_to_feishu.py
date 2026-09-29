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
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_BASE_TOKEN = "LYdZbR3DTaFPeYsHP8ScqPVCnFe"
DEFAULT_TABLE = "帖子主题"
BATCH = 200  # lark-cli 单次最大 200

# 关键：必须用创建该 Base 的 app profile（claw / cli_aa0112b836bf5be2）。
# 默认 active profile 是 huidu（cli_aa20b02703b99d18），对这张表无权限（91403）。
# 命名 profile 见 ~/.lark-cli/config.json（config init --name claw）。
LARK_PROFILE = os.environ.get("LARK_PROFILE", "claw")
CACHE_FILE = Path(__file__).resolve().parent / "data" / "feishu_topic_ids.json"
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
        def fmt(ts):
            if not ts:
                return ""
            s = str(ts).replace("T", " ").replace("Z", "").replace("+00:00", "")
            return s[:19]
        out.append({
            "标题": r.get("title", ""),
            "帖子链接": "https://linux.do" + (r.get("url") or ""),
            "作者": r.get("author", ""),
            "板块": r.get("category", ""),
            "回复数": int(r.get("replies", 0) or 0),
            "浏览量": int(r.get("views", 0) or 0),
            "发布时间": fmt(r.get("created_at")),
            "最近活跃": fmt(r.get("bumped_at")),
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


def push(base_token, table, rows, dry_run=False, known_ids=None):
    total = len(rows)
    if total == 0:
        print("无待写入记录")
        return 0
    print(f"共 {total} 条，分 { (total + BATCH - 1)//BATCH } 批（每批 ≤{BATCH}）")
    written = 0
    for i in range(0, total, BATCH):
        chunk = rows[i:i + BATCH]
        payload = json.dumps({"create_records": chunk}, ensure_ascii=False)
        cmd = [
            "lark-cli", "--profile", LARK_PROFILE, "base", "+record-batch-create",
            "--base-token", base_token,
            "--table-id", table,
            "--json", payload,
        ]
        if dry_run:
            cmd.append("--dry-run")
        print(f"  写入第 {i//BATCH+1} 批（{len(chunk)} 条）...", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ❌ 失败: {r.stderr[:500]}", file=sys.stderr)
            break
        try:
            res = json.loads(r.stdout)
            # lark-cli +record-batch-create 成功返回：data 可能是 dict（含 records/record_id_list）或 list
            data = res.get("data")
            if isinstance(data, dict):
                created = len(data.get("records") or data.get("record_id_list") or [])
            elif isinstance(data, list):
                created = len(data)
            else:
                created = len(chunk)
        except Exception:
            created = len(chunk)
        written += created
        if known_ids is not None and created == len(chunk):
            known_ids.update(str(row.get("Topic ID")) for row in chunk if row.get("Topic ID"))
            save_id_cache(known_ids)
        print(f"  ✅ 已写 {created} 条")
    print(f"完成：写入 {written}/{total} 条 → 表 [{table}]")
    return written


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

    # 幂等清洗：帖子链接若是对象则转纯字符串（url 字段应存裸 URL）；标签缺失则补空
    cleaned = []
    for r in rows:
        r = dict(r)
        link = r.get("帖子链接")
        if isinstance(link, dict):
            r["帖子链接"] = link.get("link") or link.get("text") or ""
        r.setdefault("标签", [])
        cleaned.append(r)
    rows = cleaned

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

    written = push(args.base_token, args.table, rows, known_ids=known_ids)
    if args.result_file:
        atomic_write_json(Path(args.result_file), {
            "requested": original, "skipped": skipped, "written": written,
        })
    if written != len(rows):
        raise SystemExit(4)


if __name__ == "__main__":
    main()
