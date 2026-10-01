#!/usr/bin/env python3
"""打印 搞七捻三 列表首行的相关 DOM，确定时间元素真实位置。"""
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
    print("title:", page.title())
    out = page.evaluate("""
    () => {
        const row = document.querySelector('tr.topic-list-item');
        if (!row) return {err: 'no row', html: document.body.innerHTML.slice(0, 500)};
        const cellTexts = {};
        row.querySelectorAll('td').forEach((td, i) => {
            cellTexts[i + ':' + td.className] = td.textContent.trim().replace(/\\s+/g, ' ').slice(0, 80);
        });
        const timeEls = [];
        row.querySelectorAll('*').forEach(el => {
            if (el.tagName === 'TIME' || el.hasAttribute('datetime') || el.hasAttribute('title')) {
                timeEls.push({
                    tag: el.tagName, cls: el.className,
                    datetime: el.getAttribute('datetime'),
                    title: el.getAttribute('title'),
                    text: el.textContent.trim().slice(0, 40),
                    parentCls: el.parentElement ? el.parentElement.className : ''
                });
            }
        });
        const bottom = row.querySelector('.link-bottom-line');
        return {
            cells: cellTexts,
            times: timeEls,
            bottomHTML: bottom ? bottom.outerHTML.slice(0, 600) : null,
            activityHTML: row.querySelector('td.activity') ? row.querySelector('td.activity').outerHTML.slice(0, 400) : null
        };
    }
    """)
    import json
    print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
finally:
    ctx.close()
