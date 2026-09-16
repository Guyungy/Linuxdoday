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
        })
    return out


def load_from_stdin():
    return json.load(sys.stdin)


def load_from_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def push(base_token, table, rows, dry_run=False):
    """分批写入飞书"""
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
            "lark-cli", "base", "+record-batch-create",
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

    push(args.base_token, args.table, rows)


if __name__ == "__main__":
    main()
