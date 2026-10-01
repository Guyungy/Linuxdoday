#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
POLL-80 step1 三臂诊断 — 臂 C（真实 Chrome + CDP attach，非 Playwright）
用 APFS 同源副本（同 arm A/B），直连出口（无代理）。

机制：直接启动系统 Google Chrome 二进制（不是 playwright 启动），
带 --remote-debugging-port=0 与 profile 副本；读 DevToolsActivePort 拿到
调试端口 + browser WS 路径；用 CDP 连上后派发 3 个只读同源 fetch 请求，
机器可读回每个 HTTP 状态码与首字节时间。

每臂只发 3 个只读请求，不遍历、不落盘、不推飞书。
用法：PROBE_PROFILE=<dir> python3 probe_c.py
"""
import os
import sys
import time
import json
import base64
import socket
import struct
import subprocess
from datetime import datetime, timezone

BASE = "https://linux.do"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE = os.environ.get("PROBE_PROFILE")
ENDPOINTS = [
    (1, "site.json", "/site.json"),
    (2, "develop4.json", "/c/develop/4.json?page=0"),
    (3, "top.json", "/top.json?period=daily"),
]
ARM = "C"

def utc():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")

def log(m):
    print(f"[{utc()}] [{ARM}] {m}", flush=True)

# --- 零依赖 WebSocket / CDP（来自 chrome-cdp-attach 技能，修正帧解析）---
class CDP:
    def __init__(self, port, path, timeout=20):
        self.port = port; self.path = path; self.timeout = timeout
        self.s = None; self._buf = b""; self._id = 0

    def connect(self):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {self.path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
               f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.s = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        self.s.settimeout(self.timeout)
        self.s.sendall(req.encode())
        # 读 HTTP 101 响应头（含 \r\n\r\n），随后字节属于 WS 帧
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.s.recv(4096)
            if not chunk:
                break
            head += chunk
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError(f"WS handshake failed: {head[:120]!r}")
        # 保留 101 头之后的帧字节
        self._buf = head.split(b"\r\n\r\n", 1)[1]

    def _send_frame(self, payload):
        mask = os.urandom(4); n = len(payload)
        hdr = bytearray([0x81])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        hdr += mask
        self.s.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _fill_buf(self, want=None):
        """持续读 socket 直到 _buf 达到 want 长度或连接关闭。"""
        while True:
            if want is not None and len(self._buf) >= want:
                return
            try:
                chunk = self.s.recv(65536)
            except socket.timeout:
                raise TimeoutError("recv timeout waiting for frame")
            if not chunk:
                raise ConnectionError("socket closed")
            self._buf += chunk

    def _recv_frame(self):
        while True:
            if len(self._buf) < 2:
                self._fill_buf(2)
            b0, b1 = self._buf[0], self._buf[1]
            fin = bool(b0 & 0x80); opcode = b0 & 0x0F
            masked = bool(b1 & 0x80); ln = b1 & 0x7F
            off = 2
            if ln == 126:
                if len(self._buf) < 4: self._fill_buf(4)
                ln = struct.unpack(">H", self._buf[2:4])[0]; off = 4
            elif ln == 127:
                if len(self._buf) < 10: self._fill_buf(10)
                ln = struct.unpack(">Q", self._buf[2:10])[0]; off = 10
            mask_key = None
            if masked:
                if len(self._buf) < off + 4: self._fill_buf(off + 4)
                mask_key = self._buf[off:off+4]; off += 4
            if len(self._buf) < off + ln:
                self._fill_buf(off + ln)
            frame = self._buf[off:off+ln]
            self._buf = self._buf[off+ln:]
            if masked and mask_key:
                frame = bytes(b ^ mask_key[i % 4] for i, b in enumerate(frame))
            if opcode == 0x8:  # close
                raise ConnectionError("WS closed")
            if opcode in (0x1, 0x2):  # text / binary
                return frame
            # ping(0x9)/pong(0xA)/continuation(0x0): 跳过, 继续
            if opcode == 0x0 and fin:
                return frame  # continuation-end 的文本帧（罕见）

    def send(self, method, params=None, sessionId=None, timeout=None):
        to = timeout or self.timeout
        self._id += 1
        msg = {"id": self._id, "method": method, "params": params or {}}
        if sessionId:
            msg["sessionId"] = sessionId
        self._send_frame(json.dumps(msg).encode())
        self.s.settimeout(to)
        deadline = time.time() + to
        while time.time() < deadline:
            data = self._recv_frame()
            try:
                obj = json.loads(data.decode(errors="replace"))
            except Exception:
                continue
            if obj.get("id") == self._id:
                return obj
            # 其它是事件, 丢弃
        raise TimeoutError(f"CDP no reply for {method}")

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def read_dp(profile):
    """用 --remote-debugging-port=0，Chrome 写 DevToolsActivePort。"""
    for _ in range(50):
        p = os.path.join(profile, "DevToolsActivePort")
        if os.path.exists(p):
            with open(p) as f:
                lines = f.read().splitlines()
            if len(lines) >= 1:
                return int(lines[0].strip()), (lines[1].strip() if len(lines) > 1 else "")
        time.sleep(0.5)
    return None, None


def run():
    profile = PROFILE
    if not profile or not os.path.isdir(profile):
        log(f"PROBE_PROFILE 无效: {profile!r}")
        sys.exit(2)

    result = {"arm": ARM, "profile": os.path.basename(profile), "proxy": "none",
              "ts_utc": utc(), "carrier": "real-chrome-cdp", "endpoints": []}

    # 启动真实 Chrome（非 Playwright），离屏，直连，开调试端口
    launch_argv = [CHROME,
                   f"--user-data-dir={profile}",
                   "--remote-debugging-port=0",
                   "--disable-blink-features=AutomationControlled",
                   "--window-position=-2400,-2400", "--window-size=1280,900",
                   "--no-first-run", "--no-default-browser-check",
                   "about:blank"]
    dp = os.path.join(profile, "DevToolsActivePort")
    if os.path.exists(dp):
        log("清理残留 DevToolsActivePort")
        os.remove(dp)
    log("启动真实 Chrome（CDP attach, 直连无代理）...")
    proc = subprocess.Popen(launch_argv, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    port, path = read_dp(profile)
    if not port:
        log("未能读取 DevToolsActivePort")
        proc.terminate()
        sys.exit(2)
    log(f"Chrome 调试端口 {port}")

    # 建新页 target 并用 CDP attach
    cdp = CDP(port, f"/devtools/browser/{path.split('/')[-1]}")
    try:
        cdp.connect()
    except Exception as e:
        log(f"CDP 连接失败: {type(e).__name__}: {e}")
        proc.terminate(); sys.exit(2)

    # 建一个空页
    tgt = cdp.send("Target.createTarget", {"url": "about:blank", "background": True})
    tid = tgt.get("result", {}).get("targetId")
    if not tid:
        log("Target.createTarget 失败")
        cdp.close(); proc.terminate(); sys.exit(2)
    attach = cdp.send("Target.attachToTarget", {"targetId": tid, "flatten": True})
    sid = attach.get("result", {}).get("sessionId")
    log(f"已 attach target {tid[:8]} sessionId {sid[:8] if sid else '?'}")

    def ev(method, params=None, timeout=90):
        return cdp.send(method, params, sessionId=sid, timeout=timeout)

    ev("Page.enable", {}, 30)
    ev("Runtime.enable", {}, 30)

    # 先导航首页建立 CF 会话（真实 Chrome，非 Playwright）
    nav_t0 = time.time()
    try:
        ev("Page.navigate", {"url": BASE}, 30)
    except Exception as e:
        log(f"navigate 异常: {e}")
    time.sleep(5)  # 给 CF 挑战/跳转时间
    log(f"导航完成（等待 CF 约 5s），累计 {time.time()-nav_t0:.2f}s")

    # 3 个只读同源 fetch，机器读状态码 + 首字节
    for num, name, path in ENDPOINTS:
        t0 = time.time()
        expr = f"""(async () => {{
            const t = performance.now();
            const r = await fetch({json.dumps(path)}, {{credentials: 'include',
                headers: {{'Accept': 'application/json'}}}});
            return {{status: r.status, ttfb: Math.round((performance.now()-t)*100)/100,
                ct: (r.headers.get('content-type')||'').substring(0,40),
                title: (document.title||'').substring(0,40)}};
        }})()"""
        try:
            out = ev("Runtime.evaluate",
                     {"expression": expr, "awaitPromise": True, "returnByValue": True, "timeout": 60000}, 90)
            res = out.get("result", {}).get("result", {})
            val = res.get("value")
            if val is None and "exceptionDetails" in out.get("result", {}):
                val = {"status": "EXC", "detail": out["result"].get("exceptionDetails", {}).get("text", "")}
        except Exception as e:
            val = {"status": "CDP-ERR", "detail": f"{type(e).__name__}: {e}"}
        wall = time.time() - t0
        s = val.get("status") if isinstance(val, dict) else val
        log(f"  {num}. {path} -> HTTP {s} ttfb={val.get('ttfb') if isinstance(val,dict) else '?'}s (wall {wall:.2f}s) ct={val.get('ct') if isinstance(val,dict) else ''}")
        result["endpoints"].append({
            "n": num, "path": path, "status": s,
            "ttfb_s": (val.get("ttfb") if isinstance(val, dict) else None),
            "wall_s": round(wall, 3),
            "content_type": (val.get("ct") if isinstance(val, dict) else None),
            "title": (val.get("title") if isinstance(val, dict) else None),
        })

    cdp.close()
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        pass
    log("已关闭真实 Chrome")
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    run()