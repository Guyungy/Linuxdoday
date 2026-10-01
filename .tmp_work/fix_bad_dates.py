#!/usr/bin/env python3
"""补齐 pending 里 29 条时间字段脏掉的记录（用 /t/<slug>/<id>.json 拿真实时间）。

用法:
  python .tmp_work/fix_bad_dates.py            # 只抓取，产出 .tmp_work/date_fix.json
  python .tmp_work/fix_bad_dates.py --apply    # 抓取后回写 pending + 本地缓存（自动备份）
"""
import json
import random
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path("/Users/a1/Code/Linuxdoday")
sys.path.insert(0, str(ROOT))
from browser_utils import start_browser, check_login  # noqa: E402

DT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
PENDING = ROOT / "data/pending_feishu.json"
CACHE = ROOT / "data/linuxdo_topics.json"
FIXFILE = ROOT / ".tmp_work/date_fix.json"


def bad_rows(rows):
    out = []
    for r in rows:
        created, bumped = str(r.get("发布时间", "")), str(r.get("最近活跃", ""))
        if not DT.match(created) or not DT.match(bumped):
            out.append(r)
    return out


def main():
    apply_back = "--apply" in sys.argv
    pending = json.loads(PENDING.read_text(encoding="utf-8"))
    targets = bad_rows(pending)
    print(f"待补时间的记录: {len(targets)}")
    for r in targets[:3]:
        print(f"  {r.get('Topic ID')} 发布时间={r.get('发布时间')!r} 最近活跃={r.get('最近活跃')!r}")
    if not targets:
        return

    ctx, page = start_browser(headless=False, offscreen=True)
    fixes = {}
    try:
        page.goto("https://linux.do/", timeout=45000)
        time.sleep(3)
        if not check_login(page):
            print("✗ 未登录，放弃（先跑 linux_do_scraper.py --browse）")
            return
        for i, row in enumerate(targets, 1):
            tid = str(row.get("Topic ID"))
            link = str(row.get("帖子链接") or "")
            m = re.search(r"/t/([^/]+)/(\d+)", link)
            slug = m.group(1) if m else "topic"
            data = page.evaluate("""
            async (args) => {
                try {
                    const r = await fetch("/t/" + args.slug + "/" + args.tid + ".json",
                                          {credentials: "include", headers: {"Accept": "application/json"}});
                    if (!r.ok) return {error: "HTTP " + r.status};
                    const j = await r.json();
                    return {created_at: j.created_at, bumped_at: j.bumped_at, last_posted_at: j.last_posted_at,
                            title: j.title};
                } catch (e) { return {error: String(e)}; }
            }
            """, {"slug": slug, "tid": tid})
            if not data or data.get("error"):
                print(f"  ✗ {tid}: {(data or {}).get('error')}")
            else:
                fixes[tid] = data
                print(f"  ✓ {tid}: created={data.get('created_at')} bumped={data.get('bumped_at')}")
            time.sleep(random.uniform(1.0, 2.0))
    finally:
        ctx.close()

    FIXFILE.write_text(json.dumps(fixes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n抓到 {len(fixes)}/{len(targets)} 条 → {FIXFILE}")
    if not apply_back:
        return

    # 回写（先备份）
    stamp = time.strftime("%Y%m%d-%H%M%S")
    def norm(ts):
        if not ts:
            return ""
        s = str(ts).replace("T", " ").replace("Z", "")
        m2 = re.match(r"^(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})", s)
        return f"{m2.group(1)} {m2.group(2)}" if m2 else ""

    changed_pending = 0
    for r in pending:
        tid = str(r.get("Topic ID"))
        fix = fixes.get(tid)
        if not fix:
            continue
        if not DT.match(str(r.get("发布时间", ""))) and norm(fix.get("created_at")):
            r["发布时间"] = norm(fix["created_at"])
            changed_pending += 1
        if not DT.match(str(r.get("最近活跃", ""))):
            bumped = norm(fix.get("bumped_at")) or norm(fix.get("last_posted_at"))
            if bumped:
                r["最近活跃"] = bumped

    shutil.copy2(PENDING, PENDING.with_suffix(f".json.bak-{stamp}"))
    PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=1), encoding="utf-8")

    cache = json.loads(CACHE.read_text(encoding="utf-8"))
    changed_cache = 0
    for r in cache:
        fix = fixes.get(str(r.get("id")))
        if not fix:
            continue
        if not DT.match(str(r.get("created_at", ""))) and norm(fix.get("created_at")):
            r["created_at"] = norm(fix["created_at"])
            changed_cache += 1
        if not DT.match(str(r.get("bumped_at", ""))):
            bumped = norm(fix.get("bumped_at")) or norm(fix.get("last_posted_at"))
            if bumped:
                r["bumped_at"] = bumped
    shutil.copy2(CACHE, CACHE.with_suffix(f".json.bak-{stamp}"))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")

    left = bad_rows(pending)
    print(f"回写完成：pending 修 {changed_pending} 条 / 缓存修 {changed_cache} 条")
    print(f"剩余非法时间字段的记录: {len(left)}")
    for r in left[:5]:
        print(f"  {r.get('Topic ID')} 发布时间={r.get('发布时间')!r} 最近活跃={r.get('最近活跃')!r}")


if __name__ == "__main__":
    main()
