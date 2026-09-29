# Docker 部署指南（自动刷帖机器人）

> **先确认你打开的是哪一套。** 本仓库有**两套用途完全不同的 Docker 配方**：
>
> | | 本目录 `docker/` | 根目录 |
> |---|---|---|
> | 文件 | `Dockerfile` + `docker-compose.yml` + `.env.example` | `Dockerfile.service` + `docker-compose.service.yml` |
> | 干什么 | **用账号密码自动刷帖/点赞（养号）** | **抓取帖子 + HTTP API 服务** |
> | Python | 3.11 | 3.12 |
> | 依赖 | `DrissionPage` + `schedule` | `curl_cffi`（browser 目标另加 `playwright`） |
> | 入口 | `linux_do_docker.py` | `service.py` |
> | 配置 | `.env` 里的账号密码 | 环境变量（`SERVICE_TOKEN` 等） |
> | 文档 | 本文件 | [根 README](../README.md)、[两套配方对照](../README.md#两套-docker-配方) |
>
> 要抓数据做仪表盘/日报，请去根目录那套；本目录只做「让账号看起来在活动」这件事。
> 两者互不依赖，可以同时部署，**但不要把两边的 compose 文件互相替换**。
>
> 另注：本目录依赖的 DrissionPage 4.x 与 Chrome 153 不兼容（WebSocket 404，
> 见 `browser_utils.py` 顶部说明）。这条路线属于 **legacy**，新部署建议优先考虑
> 根目录的服务方案。

## 飞牛NAS / 任意 Docker 环境部署

### 快速开始

```bash
# 1. 进入 docker 目录
cd docker

# 2. 复制配置文件，填入账号密码
cp .env.example .env
nano .env

# 3. 启动
docker-compose up -d

# 4. 查看日志
docker-compose logs -f
```

### 配置说明

编辑 `.env` 文件：

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `LINUXDO_USERNAME` | ✅ | - | Linux.do 用户名 |
| `LINUXDO_PASSWORD` | ✅ | - | Linux.do 密码 |
| `RUNS_PER_DAY` | ❌ | 2 | 每天运行次数 |
| `TOPICS_MIN` | ❌ | 15 | 每次最少浏览帖子数 |
| `TOPICS_MAX` | ❌ | 40 | 每次最多浏览帖子数 |
| `LIKE_RATE` | ❌ | 30 | 点赞概率 (0-100) |
| `RUN_ON_START` | ❌ | true | 启动时是否立即运行一次 |

### 运行机制

- 启动后立即执行一次浏览任务
- 之后每天在 7:00-23:00 之间随机选择时间运行
- 每次运行时间、浏览数量、点赞都是随机的
- 浏览器数据持久化，登录状态会保持

### 常用命令

```bash
# 启动
docker-compose up -d

# 停止
docker-compose down

# 查看日志
docker-compose logs -f

# 重启
docker-compose restart

# 重新构建（更新代码后）
docker-compose up -d --build

# 只运行一次（不启动调度器）
docker-compose run --rm linuxdo --once
```

### 资源占用

- 内存限制: 1GB
- CPU 限制: 1 核
- 磁盘: Chrome 数据约 200MB

### 和其他配方的边界

- 本配方**不提供** HTTP API、不做数据落盘、不写飞书 —— 它只是登录后随机浏览/点赞。
- 抓取帖子、正文、热榜、AI 日报、飞书同步，全部在根目录那套里（见
  [根 README](../README.md)）。
- 两套都装了 Chrome，但来源与版本要求不同：本配方用 `docker/Dockerfile` 里的
  `google-chrome-stable`，根目录的 `browser` 目标同理但 Python 版本更高。
