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

DEFAULT_BASE_TOKEN = "LYdZbR3DTaFPeYsHP8ScqPVCnFe"
DEFAULT_TABLE = "帖子主题"
BATCH = 200  # lark-cli 单次最大 200

# 关键：必须用创建该 Base 的 app profile（claw / cli_aa0112b836bf5be2）。
# 默认 active profile 是 huidu（cli_aa20b02703b99d18），对这张表无权限（91403）。
# 命名 profile 见 ~/.lark-cli/config.json（config init --name claw）。
LARK_PROFILE = "claw"


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
            "摘要": r.get("excerpt", ""),
        })
    return out


def load_from_stdin():
    return json.load(sys.stdin)


def load_from_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def existing_topic_ids(base_token, table):
    """查询飞书表中已存在的 Topic ID 集合（幂等去重用）"""
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
    return ids


def dedupe_against_feishu(base_token, table, rows):
    """按 Topic ID 剔除飞书表中已存在的行（幂等双保险）"""
    ids = existing_topic_ids(base_token, table)
    if ids is None:
        return rows, 0
    kept = [r for r in rows if str(r.get("Topic ID", "")) not in ids]
    skipped = len(rows) - len(kept)
    return kept, skipped


def push(base_token, table, rows, dry_run=False):
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
        print(f"  ✅ 已写 {created} 条")
    print(f"完成：写入 {written}/{total} 条 → 表 [{table}]")
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-token", default=DEFAULT_BASE_TOKEN)
    ap.add_argument("--table", default=DEFAULT_TABLE)
    ap.add_argument("--file", default="", help="从本地 JSON 文件读取")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

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
    rows, skipped = dedupe_against_feishu(args.base_token, args.table, rows)
    if skipped:
        print(f"已跳过 {skipped} 条已入库记录（按 Topic ID 去重）")
    if not rows:
        print("无新增记录")
        return

    push(args.base_token, args.table, rows)


if __name__ == "__main__":
    main()
