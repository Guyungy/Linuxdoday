#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
POLL-80 step1 三臂诊断 — 臂 A（现状）/ 臂 B（删 CF 会话 cookie）
用 APFS 写时复制同源副本，直连出口（无代理，与线上一致）。

每臂只发 3 个只读请求，记录状态码 + 首字节时间，不遍历、不落盘、不推飞书。
用法：PROBE_PROFILE=<dir> python3 probe_ab.py A|B
"""
import os
import sys
import time
import json
import shutil
import sqlite3
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

BASE = "https://linux.do"
PROFILE = os.environ.get("PROBE_PROFILE")
ENDPOINTS = [
    (1, "site.json", "/site.json"),
    (2, "develop4.json", "/c/develop/4.json?page=0"),
    (3, "top.json", "/top.json?period=daily"),
]
ARM = sys.argv[1] if len(sys.argv) > 1 else "A"

def utc():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log(m):
    print(f"[{utc()}] [{ARM}] {m}", flush=True)

def strip_cf_cookies(profile):
    """臂 B：删除 CF 会话相关 cookie（cf_clearance / __cf_bm / cf_chl_* / _cfuvid / __cf_ob）。
    直接在 profile 的 Cookies DB 上删除目标行 —— 让浏览器在无 CF 会话的干净状态下发探测，
    测「会话是否卡点」。DB 需以可写方式打开（Chrome 未运行时无锁）。"""
    db = os.path.join(profile, "Default", "Cookies")
    if not os.path.exists(db):
        log("Cookie DB 不存在，跳过删除")
        return 0
    con = sqlite3.connect(db)
    cur = con.cursor()
    targets = ["cf_clearance", "__cf_bm", "cf_chl_", "_cfuvid", "__cf_ob"]
    clause = " OR ".join(["name LIKE ?"] * len(targets))
    before = cur.execute("SELECT count(*) FROM cookies").fetchone()[0]
    cur.execute("DELETE FROM cookies WHERE (" + clause + ")", [f"%{t}%" for t in targets])
    con.commit()
    after = cur.execute("SELECT count(*) FROM cookies").fetchone()[0]
    con.close()
    # 清掉 WAL/SHM 残留，避免 Chrome 读到已删 cookie 的老 WAL
    for suf in ("-wal", "-shm"):
        p = db + suf
        if os.path.exists(p):
            os.remove(p)
    log(f"臂 B：cookie {before} -> {after}（删除 {before-after} 条 CF 会话 cookie）")
    return before - after

def run():
    profile = PROFILE
    if not profile or not os.path.isdir(profile):
        log(f"PROBE_PROFILE 无效: {profile!r}")
        sys.exit(2)

    if ARM == "B":
        strip_cf_cookies(profile)

    p = sync_playwright().start()
    result = {"arm": ARM, "profile": os.path.basename(profile), "proxy": "none",
              "ts_utc": utc(), "endpoints": []}
    try:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile,
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--window-position=-2400,-2400", "--window-size=1280,900"],
            viewport={"width": 1920, "height": 1080},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        log("浏览器已启动（Playwright persistent, chrome, 离屏, 直连无代理）")

        # 先访问首页建立 CF 会话（真实导航）
        nav_start = time.time()
        try:
            page.goto(BASE, timeout=60000, wait_until="domcontentloaded")
            log(f"导航 {BASE} 完成，耗时 {time.time()-nav_start:.2f}s")
        except Exception as e:
            log(f"导航异常: {type(e).__name__}: {e}")

        for num, name, path in ENDPOINTS:
            t0 = time.time()
            try:
                status = page.evaluate(
                    """async (path) => {
                        try {
                            const r = await fetch(path, {credentials: "include",
                                headers: {"Accept": "application/json"}});
                            return {status: r.status, ct: (r.headers.get('content-type')||'').slice(0,40),
                                    title: (document.title || '').slice(0,40)};
                        } catch (e) { return {status: "ERR:"+e, ct: "", title: ""}; }
                    }""", path)
            except Exception as e:
                status = {"status": "EXC:" + type(e).__name__, "ct": "", "title": ""}
            ttfb = time.time() - t0
            s = status.get("status"); ct = status.get("ct"); title = status.get("title", "")
            log(f"  {num}. {path} -> HTTP {s} (ct={ct}) ttfb={ttfb:.3f}s {('[title] '+title) if title else ''}")
            result["endpoints"].append({"n": num, "path": path, "status": s,
                                        "content_type": ct, "title": title, "ttfb_s": round(ttfb, 3)})

        ctx.close()
    except Exception as e:
        log(f"ARM 异常: {type(e).__name__}: {e}")
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            p.stop()
        except Exception:
            pass

    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    run()