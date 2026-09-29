# Linuxdoday 后台服务

Linuxdoday 是一个可部署在 Linux 服务器、NAS、Docker、云主机或本地电脑上的 Linux.do 数据抓取后台服务。

![Tests](https://github.com/Guyungy/Linuxdoday/actions/workflows/test.yml/badge.svg)

默认使用 RSS 通道，不需要桌面环境、Chrome 或浏览器自动化；切到 `SCRAPE_MODE=browser` 则改用 playwright 离屏真实 Chrome，可拿到浏览量、回复数等完整指标。服务会定时抓取最新帖子，保存增量缓存，并通过 HTTP API 提供健康状态、运行状态、最新结果和手动触发能力。

## 两种抓取模式

| 模式 | 指标 | 速度 | 依赖 |
|---|---|---|---|
| `rss`（默认） | 标题/作者/时间/首帖正文，**无浏览量、回复数** | 每板块约 0.5–1.2s / 25 条 | `curl_cffi` |
| `browser` | 上表全部 **+ 浏览量、回复数、点赞** | 每板块约 10–20s / 30–90 条 | `playwright` + 已安装的 Google Chrome + 图形会话 |

实测：RSS 的 `?page=N` 分页有效（第 2 页返回更旧的 25 条），但连续请求极易触发 **HTTP 429**，默认 4–7s 间隔偏激进；`--rss-pages` 建议保持 `1`。browser 模式走登录态浏览器内的 JSON 接口，不受 RSS 限流影响，是拿完整指标的唯一方式（headless 会被 Cloudflare 403）。

### 抓取深度

browser 模式每页 30 条，板块列表**本身没有 40 页上限**（实测「搞七捻三」翻到 80 页仍未到底，2,400 条 / 128s），深度完全由 `SCRAPE_MAX_PAGES` 决定。每页约 2–4s，按板块数线性增长：

| `SCRAPE_MAX_PAGES` | 每板块条数 | 14 个板块的单轮耗时 | 适用 |
|---:|---:|---:|---|
| 3（默认） | ~90 | ~3 分钟 | 只要最新增量 |
| 10 | ~300 | ~8 分钟 | 6 小时一轮，覆盖高速板块的完整窗口 |
| 40（`--full`） | ~1,200 | ~30 分钟 | 一次性回溯历史，注意首次会把大量历史帖判为「新增」并推飞书 |

## 快速部署：Docker Compose

```bash
git clone https://github.com/Guyungy/Linuxdoday.git
cd Linuxdoday

# 为手动触发接口设置一个随机令牌
export SERVICE_TOKEN="replace-with-a-long-random-token"

docker compose -f docker-compose.service.yml up -d --build
```

检查服务：

```bash
curl http://127.0.0.1:8080/health
curl http://127.0.0.1:8080/status
curl http://127.0.0.1:8080/topics
```

手动触发一次后台抓取：

```bash
curl -X POST http://127.0.0.1:8080/run \
  -H "Authorization: Bearer $SERVICE_TOKEN"
```

## 直接运行

适用于 Linux、macOS 或其他安装了 Python 3.9+ 的环境：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-service.txt
python service.py
```

需要完整指标（浏览量/回复数）时，改用 browser 模式并安装浏览器依赖：

```bash
pip install -r requirements.txt          # playwright
python linux_do_scraper.py --browse      # 首次人工登录，登录态存入 browser_data/
SCRAPE_MODE=browser python service.py
```

browser 模式下抓取窗口默认移到屏幕外（`--window-position=-2400,-2400`），不抢焦点、不遮挡桌面；只有 `--browse` 才会弹出可见窗口。

只执行一次、不启动 HTTP 服务：

```bash
python service.py --once
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 存活检查，供 Docker/Kubernetes 使用 |
| `GET` | `/ready` | 就绪检查，服务初始化完成后返回 200 |
| `GET` | `/status` | 当前状态、最近成功时间、运行次数和错误信息 |
| `GET` | `/topics` | 最近一次运行发现的新增帖子 |
| `GET` | `/hot` | 官方热榜（`/top.json` 的 daily/weekly/monthly），由抓取轮次写入 |
| `POST` | `/run` | 异步触发抓取，需要 Bearer Token |

当没有配置 `SERVICE_TOKEN` 时，`POST /run` 会被禁用，定时任务仍正常运行。

## 配置

全部通过环境变量配置，方便 Docker、systemd、Kubernetes 和各类 PaaS 使用。

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `SERVICE_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `SERVICE_PORT` | `8080` | HTTP 端口 |
| `SERVICE_TOKEN` | 空 | 手动触发令牌；为空时禁用 `/run` |
| `PROTECT_READ_ENDPOINTS` | `false` | 是否也用 Bearer Token 保护 `/status` 和 `/topics` |
| `SCRAPE_INTERVAL_SECONDS` | `21600` | 抓取间隔，默认 6 小时，最小 60 秒 |
| `SCRAPE_TIMEOUT_SECONDS` | `1800` | 单次抓取超时 |
| `RUN_ON_START` | `true` | 服务启动后是否立即抓取 |
| `SCRAPE_MODE` | `rss` | `rss` 或 `browser`；`browser` 需要 `playwright` 与图形会话 |
| `SCRAPE_MAX_PAGES` | `0` | `browser` 模式每板块翻页数；0 = 默认 3 页（约 90 条），`--full` 等价 40 页 |
| `SCRAPE_CATEGORIES` | 空 | 板块名逗号分隔；为空使用默认启用板块 |
| `SCRAPE_LIMIT` | `0` | 每个板块最多抓取数量；0 表示 RSS 默认数量。注意按“页”生效，结果可能多于该值 |
| `SCRAPE_TOTAL_LIMIT` | `0` | 一轮全部板块合计最多处理数量；**在增量去重之前截断**，非 0 时排在后面的板块会被整体丢弃 |
| `SCRAPE_RSS_PAGES` | `1` | 每个板块读取的 RSS 页数（仅 `rss` 模式；≥2 容易 429） |
| `SCRAPE_CONTENT` | `false` | 是否抓取新帖正文（存入正文缓存，并随该行一起写入飞书） |
| `SCRAPE_CONTENT_LIMIT` | `0` | 单轮最多抓 N 条正文；0 = 不限。建议设为几百，避免大轮次把 `SCRAPE_TIMEOUT_SECONDS` 耗尽 |
| `SCRAPE_HOT` | browser 模式默认 `true` | 是否同时抓官方热榜（1 次请求）写入 `data/hot_topics.json` |
| `LINUXDO_PROXY` | 空 | 可选 HTTP 代理 |
| `PUSH_TO_FEISHU` | `false` | 抓取后自动调用本机 `lark-cli` 写入飞书 |
| `LARK_PROFILE` | `claw` | 飞书 `lark-cli` 配置名 |
| `FEISHU_ID_CACHE_SECONDS` | `86400` | 飞书 Topic ID 本地索引有效期，避免每轮扫全表 |
| `DAILY_REPORT` | `false` | 是否每天生成一份 AI 日报到 `reports/`（每天只生成一次，靠当天文件去重） |
| `DAILY_REPORT_HOUR` | `8` | 当天几点之后才允许生成日报 |
| `DAILY_REPORT_DAYS` | `3` | 数据窗口天数（近 N 天滚动窗口，默认 3） |
| `DAILY_REPORT_CHAT` | 空 | 日报推送到哪个飞书会话（`oc_xxx` 群，或与自己的私聊 `chat_id`） |
| `DAILY_REPORT_MODEL` | `deepseek-flash` | 生成「分析」部分用的模型 |
| `DAILY_REPORT_PUSH_AS` | `auto` | 推送身份：`user` / `bot`（外部群发不了，机器人私聊用 `bot`） |
| `DAILY_REPORT_CARD` | `true` | 以飞书互动卡片（Card 2.0）推送，而不是 Markdown 消息 |

## AI 日报

把每天的抓取结果汇总成一份带分析的中文日报：

```bash
python daily_report.py                          # 近 3 天日报 → reports/AI日报-YYYY-MM-DD.md
python daily_report.py --days 1                 # 只看当天
python daily_report.py --date 2026-09-28        # 指定窗口截止日期
python daily_report.py --no-llm                 # 不调模型，只出统计版
python daily_report.py --push-chat oc_xxx       # 生成后推送到飞书
python daily_report.py --push-only --push-chat oc_xxx   # 把已有日报文件推送到飞书（不重新生成）
python daily_report.py --push-only --dry-run    # 只看推送内容
python daily_report.py --push-only --card --push-chat oc_xxx   # 推送为飞书互动卡片
```

报告结构（按信息价值排序，不含数据概览）：

- `⚡ 高价值信息` —— 每条三段式：**发生了什么 / 为什么重要 / 来源**，配语义色标签（红=风险、绿=利好、蓝=中性）
- `🔥 热点分析` —— 3-4 个热点，每个「现象 → 数据 → 判断」
- `📌 话题汇总` —— 按主题聚类（每帖只归一个主题）+ 代表帖
- `🔎 其它高热帖` + 官方日榜

卡片版按飞书 Card 2.0 规范构造：单个 `collapsible_panel` 承载每组信息、高价值信息默认展开并用 `blue-50` 背景强调、其余折叠。

设计要点：

- **数字由程序写死**：总量、板块分布、参与度分层、主题命中率、榜单全部本地确定性计算，
  模型只负责写「分析」叙事，prompt 里明确禁止编造统计数字。
- **没有模型也能跑**：找不到 API key 或调用失败时自动降级为统计版，不阻塞。
- 凭据优先级：环境变量 `DAILY_REPORT_API_KEY` / `DAILY_REPORT_BASE_URL` / `DAILY_REPORT_MODEL`，
  否则回退读 `~/.dsh/.credentials.yaml` 的 `DEEPSEEK_API_KEY`。
- 推送到飞书需要 `lark-cli` 具备 `im:message.send_as_user` 权限；**外部群（跨租户）发不进去**，
  用内部群或与自己的私聊。


示例：

```bash
export SCRAPE_INTERVAL_SECONDS=3600
export SCRAPE_MODE=browser
export SCRAPE_CATEGORIES="开发调优,前沿快讯"
export SCRAPE_LIMIT=10
export SERVICE_TOKEN="your-secret"
export PUSH_TO_FEISHU=true
python service.py
```

## 数据持久化

服务在 `data/` 下维护：

- `linuxdo_topics.json`：按 Topic ID 合并的历史缓存。
- `latest_run.json`：最近一次运行新增的帖子，也是 `/topics` 的数据来源。
- `topic_content.json`：正文缓存，`{topic_id: {content, fetched_at}}`。

### 正文（帖子内容）

正文是「每帖一次请求」，与列表抓取节奏不同，因此单独成环：

```bash
# 回填最近 7 天的正文（增量，只补没有的；新的优先）
python fetch_content.py --recent-days 7 --no-proxy

# 把本地正文写进飞书表中「已有」的记录（不会新建行；已有正文的行自动跳过）
python push_to_feishu.py --sync-content
```

`push_to_feishu.py` 默认只做 create + 按 Topic ID 去重，**对已存在的行不会写入正文**，所以历史行必须靠 `--sync-content` 补。开启 `SCRAPE_CONTENT` 后，新帖会在建行时就带上正文，无需再同步。

### 官方热榜

不用从板块列表里自己估热度，直接取 Discourse 的 `/top.json`（一次请求 50 条，含浏览量/回复数/点赞数）：

```bash
python hot_topics.py                        # 日榜 + 周榜 → data/hot_topics.json
python hot_topics.py --periods daily --limit 20
curl -s http://127.0.0.1:8080/hot            # 服务启动后可直接读
```

browser 模式抓取时会顺带更新热榜（复用同一个浏览器会话，不额外启动 Chrome），可用 `SCRAPE_HOT=false` 关闭。

- `pending_feishu.json`：飞书同步失败时的待重试队列，成功后自动清理。
- `feishu_topic_ids.json`：飞书去重索引，显著减少大表重复扫描。

> 自动写入飞书需要运行环境已安装并配置 `lark-cli`。默认 Docker 镜像只负责抓取和 HTTP API，不内置个人飞书凭据。

Docker Compose 使用命名卷 `linuxdoday-data` 保存数据，更新或重建容器不会丢失。

## systemd 部署

克隆并安装依赖后，可创建 `/etc/systemd/system/linuxdoday.service`：

```ini
[Unit]
Description=Linuxdoday background service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=linuxdoday
WorkingDirectory=/opt/Linuxdoday
Environment=SERVICE_PORT=8080
Environment=SCRAPE_INTERVAL_SECONDS=21600
EnvironmentFile=-/etc/linuxdoday.env
ExecStart=/opt/Linuxdoday/.venv/bin/python service.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

然后执行：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now linuxdoday
sudo systemctl status linuxdoday
```

## macOS launchd 部署

browser 模式必须在**图形会话**中运行（launchd 用户代理满足这一点）。`~/Library/LaunchAgents/com.guyungy.linuxdoday.plist` 关键片段：

```xml
<key>ProgramArguments</key>
<array>
  <!-- 必须指向已安装 playwright 的解释器，不能用没装依赖的系统 python3 -->
  <string>/Users/a1/Code/Linuxdoday/.venv/bin/python</string>
  <string>/Users/a1/Code/Linuxdoday/service.py</string>
</array>
<key>EnvironmentVariables</key>
<dict>
  <key>SERVICE_PORT</key><string>8081</string>
  <key>SCRAPE_MODE</key><string>browser</string>
  <key>SCRAPE_INTERVAL_SECONDS</key><string>21600</string>
  <key>PUSH_TO_FEISHU</key><string>true</string>
  <key>LARK_PROFILE</key><string>claw</string>
  <key>SERVICE_TOKEN</key><string>你的随机令牌</string>
  <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
</dict>
```

重载：

```bash
launchctl bootout gui/$(id -u)/com.guyungy.linuxdoday 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.guyungy.linuxdoday.plist
launchctl list | grep linuxdoday
```

> `PATH` 必须包含 `lark-cli` 所在目录（Homebrew 为 `/opt/homebrew/bin`），否则飞书推送会失败并让整轮状态变成 `error`。

## Kubernetes / PaaS

使用仓库根目录的 `Dockerfile.service` 构建镜像。容器监听 `8080`，健康检查路径为 `/health`，持久化目录为 `/app/data`。Render、Railway、Fly.io、Kubernetes、群晖 Container Manager 等平台都可以使用同一镜像。

## 说明

- RSS 通道每个板块通常返回最新约 25 条，包含标题、作者、发布时间和首帖正文，但没有浏览量、回复数等完整指标。
- 浏览器版脚本仍保留用于本地完整抓取，但不属于后台服务镜像，也不会被 Docker 安装。
- 请合理设置抓取间隔并遵守 Linux.do 社区规则。

## License

MIT
