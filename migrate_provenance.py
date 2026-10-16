#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""migrate_provenance.py — 给缓存老行补上「来源」（source / metrics_at）。

为什么要迁移：source 与 metrics_at 现在是行上的正式字段（common.SOURCE_*），
但缓存里 37,729 行是在这两个字段存在之前写下的。不迁移也能跑 ——
common.source_of() 会按形状回推来源 —— 但回推只是**老数据的兜底**：
它靠「有指标就算 browser」这类巧合，而巧合会破（实测缓存里就有一行浏览器采到
的、指标真的是 0 的新帖，id 2984019）。迁移之后缓存里存的是显式事实，
形状回推退回到它该待的位置：读一份没有来源字段的外来数据时才用。

metrics_at 一律写空串，**不编造时间**。「这批指标是什么时候看的」对老行是
未知的，写上空串就是如实说「不知道」；拿迁移时刻顶上等于谎报「刚测过」，
正是这一轮要修的那类谎。空串 + source 二者合起来语义无歧义：
  source=rss,  metrics_at=""  → 从没测过（指标恒 0 是采集口径，不是热度）
  source=browser/dom/hot, metrics_at=""  → 测过，但观测时刻不可考
后续任何一轮真实采集都会把 metrics_at 刷新成当轮时刻（见 merge_incremental）。

用法：
  python3 migrate_provenance.py --dry-run          # 只统计会怎么改，不落盘
  python3 migrate_provenance.py                    # 落盘（先自动备份到 .bak-*）
  python3 migrate_provenance.py --revert --backup data/linuxdo_topics.json.bak-provenance-<ts>
                                                   # 回滚：删掉两个新字段，或从备份还原

回滚说明：本迁移**只新增两个键**，不改任何既有值，所以 --revert（删键）在
语义上是精确的反操作。唯一的例外是迁移之后又跑过采集轮次：那些轮次会往
老行上写**真实**的 metrics_at，--revert 会连着把真数据一起删掉。要那种回滚
就用 --backup 从备份文件整份还原。两种情况脚本都会先把当前文件另存一份。
"""

import argparse
import json
import os
import shutil
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402  （必须在 sys.path 调整之后）

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(ROOT, "data", "linuxdo_topics.json")
PROVENANCE_KEYS = ("source", "metrics_at")


def stamp(row):
    """返回补好来源字段的新行（不改原 dict）。

    已有合法 source 的行原样保留 —— 迁移必须幂等，而且不能在第二次运行时
    把迁移之后真实采集写下的 dom/hot 来源降级回回推得到的 browser。
    metrics_at 只在**缺失**时补空串；已有值（哪怕是 ""）不动。
    """
    source = row.get("source")
    if source not in common.VALID_SOURCES:
        source = common.source_of(row)
    out = dict(row)
    out["source"] = source
    if "metrics_at" not in out:
        out["metrics_at"] = ""
    return out


def migrate(rows):
    """批量补来源。返回 (新行列表, 各来源计数)。"""
    out = [stamp(r) for r in rows]
    return out, Counter(r["source"] for r in out)


def revert(rows):
    """删掉来源两件套 —— 迁移的精确反操作。"""
    out = []
    for r in rows:
        r = dict(r)
        for key in PROVENANCE_KEYS:
            r.pop(key, None)
        out.append(r)
    return out


def _load(path):
    with open(path, "r", encoding="utf-8") as handle:
        rows = json.load(handle)
    if not isinstance(rows, list):
        raise SystemExit(f"FATAL: {path} 顶层不是列表，拒绝迁移")
    return rows


def _backup(path):
    stamp_name = common.now_iso().replace(":", "").replace("-", "")
    target = f"{path}.bak-provenance-{stamp_name}"
    shutil.copy2(path, target)
    return target


def main():
    ap = argparse.ArgumentParser(description="给缓存行补来源（source / metrics_at）")
    ap.add_argument("--file", default=CACHE_FILE, help="目标缓存文件")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不落盘")
    ap.add_argument("--revert", action="store_true", help="删掉来源字段（回滚）")
    ap.add_argument("--from-backup", help="整份还原备份文件，而非删键回滚")
    args = ap.parse_args()

    if args.from_backup:
        if args.dry_run:
            raise SystemExit("FATAL: --from-backup 与 --dry-run 互斥")
        rows = _load(args.from_backup)
        target = _backup(args.file)
        common.write_json_atomic(args.file, rows)
        print(f"已从 {args.from_backup} 还原 {len(rows)} 行 → {args.file}")
        print(f"（还原前另存: {target}）")
        return

    rows = _load(args.file)
    before = sum(1 for r in rows if "source" in r)

    if args.revert:
        out = revert(rows)
        summary = f"已删除 source/metrics_at（{len(out)} 行）"
    else:
        out, counts = migrate(rows)
        summary = "来源分布: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        measured = sum(1 for r in out if common.is_measured(r))
        summary += f" | 测过={measured} 没测过={len(out) - measured}"

    print(f"行数={len(rows)} 迁移前已带 source 的行={before}")
    print(summary)

    if args.dry_run:
        print("--dry-run：未落盘")
        return

    target = _backup(args.file)
    common.write_json_atomic(args.file, out)
    print(f"已写入 {args.file}（备份: {target}）")


if __name__ == "__main__":
    main()
