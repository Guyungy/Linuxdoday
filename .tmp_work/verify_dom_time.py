#!/usr/bin/env python3
"""验证 scrape_category_dom 的时间提取修复：对 搞七捻三 抓一页，逐条检查时间是否可解析。

对照对象：pending_feishu.json 里那 29 条脏行的 Topic ID。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path("/Users/a1/Code/Linuxdoday")
sys.path.insert(0, str(ROOT))

import linux_do_scraper as S  # noqa: E402
from browser_utils import start_browser  # noqa: E402

DT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

pending = json.loads((ROOT / "data/pending_feishu.json").read_text(encoding="utf-8"))
bad_ids = {
    str(r.get("Topic ID"))
    for r in pending
    if not DT.match(str(r.get("发布时间", "")).replace("T", " ")[:19] + ":00")
    and not DT.match(str(r.get("发布时间", "")))
}
print(f"目标脏行 Topic ID 数: {len(bad_ids)}")

cat = next(c for c in S.CATS if c["n"] == "搞七捻三")
ctx, page = start_browser(headless=False, offscreen=True)
try:
    topics = S.scrape_category_dom(page, cat, limit=0)
finally:
    ctx.close()

print(f"\n抓到 {len(topics)} 条")
for t in topics[:8]:
    print(f"  {t.get('id'):>8}  created={t.get('created_at')!r:30} bumped={t.get('bumped_at')!r}")
ok = sum(1 for t in topics if DT.match(str(t.get("created_at") or "")))
print(f"created_at 可解析: {ok}/{len(topics)}")
ok_b = sum(1 for t in topics if DT.match(str(t.get("bumped_at") or "")))
print(f"bumped_at 可解析: {ok_b}/{len(topics)}")

hit = [t for t in topics if str(t.get("id")) in bad_ids]
print(f"\n命中目标脏行: {len(hit)}/{len(bad_ids)}")
for t in hit[:35]:
    print(f"  {t['id']:>8}  created={t.get('created_at')!r:26} bumped={t.get('bumped_at')!r}")

miss = bad_ids - {str(t.get("id")) for t in topics}
if miss:
    print(f"\n本页未覆盖（不在第一页）: {sorted(miss)}")
