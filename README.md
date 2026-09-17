# Linuxdoday 后台服务

Linuxdoday 是一个可部署在 Linux 服务器、NAS、Docker、云主机或本地电脑上的 Linux.do 数据抓取后台服务。

默认使用 RSS 通道，不需要桌面环境、Chrome 或浏览器自动化。服务会定时抓取最新帖子，保存增量缓存，并通过 HTTP API 提供健康状态、运行状态、最新结果和手动触发能力。

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

只执行一次、不启动 HTTP 服务：

```bash
python service.py --once
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 存活检查，供 Docker/Kubernetes 使用 |
| `GET` | `/status` | 当前状态、最近成功时间、运行次数和错误信息 |
| `GET` | `/topics` | 最近一次运行发现的新增帖子 |
| `POST` | `/run` | 异步触发抓取，需要 Bearer Token |

当没有配置 `SERVICE_TOKEN` 时，`POST /run` 会被禁用，定时任务仍正常运行。

## 配置

全部通过环境变量配置，方便 Docker、systemd、Kubernetes 和各类 PaaS 使用。

| 环境变量 | 默认值 | 说明 |
|---|---:|---|
| `SERVICE_HOST` | `0.0.0.0` | HTTP 监听地址 |
| `SERVICE_PORT` | `8080` | HTTP 端口 |
| `SERVICE_TOKEN` | 空 | 手动触发令牌；为空时禁用 `/run` |
| `SCRAPE_INTERVAL_SECONDS` | `21600` | 抓取间隔，默认 6 小时，最小 60 秒 |
| `SCRAPE_TIMEOUT_SECONDS` | `1800` | 单次抓取超时 |
| `RUN_ON_START` | `true` | 服务启动后是否立即抓取 |
| `SCRAPE_CATEGORIES` | 空 | 板块名逗号分隔；为空使用默认启用板块 |
| `SCRAPE_LIMIT` | `0` | 每个板块最多抓取数量；0 表示 RSS 默认数量 |
| `SCRAPE_TOTAL_LIMIT` | `0` | 一轮全部板块合计最多处理数量 |
| `SCRAPE_RSS_PAGES` | `1` | 每个板块读取的 RSS 页数 |
| `SCRAPE_CONTENT` | `false` | 是否把 RSS 首帖正文写入正文缓存 |
| `LINUXDO_PROXY` | 空 | 可选 HTTP 代理 |
| `PUSH_TO_FEISHU` | `false` | 抓取后自动调用本机 `lark-cli` 写入飞书 |

示例：

```bash
export SCRAPE_INTERVAL_SECONDS=3600
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
- `topic_content.json`：启用 `SCRAPE_CONTENT` 后的正文缓存。

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

## Kubernetes / PaaS

使用仓库根目录的 `Dockerfile.service` 构建镜像。容器监听 `8080`，健康检查路径为 `/health`，持久化目录为 `/app/data`。Render、Railway、Fly.io、Kubernetes、群晖 Container Manager 等平台都可以使用同一镜像。

## 说明

- RSS 通道每个板块通常返回最新约 25 条，包含标题、作者、发布时间和首帖正文，但没有浏览量、回复数等完整指标。
- 浏览器版脚本仍保留用于本地完整抓取，但不属于后台服务镜像，也不会被 Docker 安装。
- 请合理设置抓取间隔并遵守 Linux.do 社区规则。

## License

MIT
