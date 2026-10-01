#!/usr/bin/env python3
"""对非置顶行核对时间元素口径。"""
import json
import sys
from pathlib import Path

ROOT = Path("/Users/a1/Code/Linuxdoday")
sys.path.insert(0, str(ROOT))
from browser_utils import start_browser  # noqa: E402

ctx, page = start_browser(headless=False, offscreen=True)
try:
    page.goto("https://linux.do/c/gossip/11", timeout=45000)
    page.wait_for_load_state("domcontentloaded")
    import time
    time.sleep(4)
    out = page.evaluate("""
    () => {
        const rows = [...document.querySelectorAll('tr.topic-list-item')].filter(r => !r.classList.contains('pinned'));
        return rows.slice(0, 5).map(row => {
            const actTd = row.querySelector('td.activity, td.num.activity');
            const stamp = row.querySelector('.relative-date[data-time]');
            const bottom = row.querySelector('.link-bottom-line');
            return {
                id: row.getAttribute('data-topic-id'),
                actTitle: actTd ? actTd.getAttribute('title') : null,
                dataTime: stamp ? stamp.getAttribute('data-time') : null,
                bottomText: bottom ? bottom.textContent.trim().slice(0, 60) : null,
                hasTimeEl: !!row.querySelector('time')
            };
        });
    }
    """)
    print(json.dumps(out, ensure_ascii=False, indent=1))
finally:
    ctx.close()
