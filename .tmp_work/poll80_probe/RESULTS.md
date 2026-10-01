# POLL-80 step1 三臂诊断结果 (2026-10-01)

出口基线: 本机唯一出口路径 = Clash TUN (utun1024) -> 所选节点 🇺🇸美国洛杉矶1号, exit IP = 198.255.5.154.
所有出站(含 --no-proxy 直连)均经 Clash TUN 出 198.255.5.154; 无独立第二出口.

三臂共用同一 browser_data 副本(APFS CoW 快照), 直连同 198.255.5.154 出口:

| 臂 | 变量 | site.json | develop/4.json | top.json |
|---|---|---|---|---|
| A 现状(Playwright+profile) | 无 | 200 | 403 | 403 |
| B 会话(删 cf_clearance/_cfuvid/__cf_ob) | 会话cookie | 200 | 403 | 403 |
| C 真实Chrome+CDP(非Playwright, 复测×2) | 载体指纹 | 200 | 403 | 403 |
| 额外对照: 纯curl(非浏览器,无指纹) | 载体=无 | 200 | 403(挑战) | 403(挑战) |

判读(判读表):
- B 与 A 同 => 会话(cf_clearance等)不是卡点
- C 也挂(真实Chrome剥离Playwright指纹仍403) => 载体指纹不是卡点
- 连纯curl(零指纹)都403/挑战 => 与载体无关
- 三臂全挂 + curl全挂 => 出口IP维度坐实: 198.255.5.154 被CF对受保护路由(develop.json/top.json)整体拦截

恢复动作: 换出口节点(极速机场订阅内已有大量备用节点, 非买线路/换线路).
Clash Verge 当前选中 🇺🇸美国洛杉矶1号; proxy-groups 内置 🇺🇸美国洛杉矶1-5/🇯🇵东京1-3/🇺🇸凤凰城/🇭🇰香港/🇹🇼台湾 等.
需在 Clash Verge 切换 GLOBAL/极速机场 组到不同 IP 节点, 并重跑一轮验证 develop.json 回到 200.
上线窗口: next_run_at = 2026-10-01T12:25:48Z, 切换应在窗口内生效.

边界: 已按保守授权只做只读探针+step1; 未擅自改线上 Clash 配置(属线上写,待明确再动).
