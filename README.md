# Linuxdoday for macOS

面向 macOS 的 Linux.do 辅助工具，支持图形化浏览、无浏览器 RSS 抓取、正文缓存和飞书日报同步。

项目可构建为标准 `Linuxdoday.app`，安装后可从 Finder、Spotlight 或启动台直接打开。推荐使用 Apple Silicon 或 Intel Mac、Python 3.9+ 与 Google Chrome。

## 功能

- macOS 图形界面：使用苹方与 Menlo 字体，保留系统原生标题栏。
- 独立浏览器资料：默认保存到 `~/Library/Application Support/Linuxdoday/browser_data`。
- Chrome 自动发现：支持系统 `/Applications` 和用户 `~/Applications` 安装位置。
- 无浏览器抓取：RSS 模式无需打开 Chrome，适合每日增量同步。
- 完整抓取：需要时通过 Chrome 获取分页、浏览量和回复数。
- 飞书同步：按 Topic ID 增量写入并进行远端幂等去重。

## 快速开始

### 1. 安装 Chrome

从 [Google Chrome 官网](https://www.google.com/chrome/) 安装，保持默认应用名称即可。

### 2. 安装项目

在终端执行：

```bash
git clone https://github.com/Guyungy/Linuxdoday.git
cd Linuxdoday
chmod +x mac_setup.sh run_mac.command
./mac_setup.sh
```

安装脚本会在项目内创建 `.venv`，不会污染系统 Python。

### 3. 构建并安装 Mac 应用

```bash
chmod +x build_mac_app.sh install_mac_app.sh
./build_mac_app.sh
./install_mac_app.sh
```

默认安装到 `~/Applications/Linuxdoday.app` 并自动打开。也可用 `./install_mac_app.sh --system` 安装到 `/Applications`。更多说明见 [MAC_APP.md](MAC_APP.md)。

### 4. 源码启动（开发调试）

双击 `run_mac.command`，或在终端执行：

```bash
./run_mac.command
```

首次出现 macOS 安全提示时，可在“系统设置 → 隐私与安全性”中允许运行。

## 帖子抓取

推荐的无浏览器模式：

```bash
.venv/bin/python linux_do_scraper.py --scrape --rss --no-proxy
```

RSS 模式每个板块提供最新约 25 条，包含标题、作者、时间和首帖正文，但没有浏览量和回复数。

需要完整指标时，先登录再抓取：

```bash
.venv/bin/python linux_do_scraper.py --browse --no-proxy
.venv/bin/python linux_do_scraper.py --scrape --no-proxy
```

常用参数：

```bash
# 指定板块
.venv/bin/python linux_do_scraper.py --scrape --rss --cats 开发调优,前沿快讯 --no-proxy

# 抓取正文
.venv/bin/python linux_do_scraper.py --scrape --rss --content --no-proxy

# 完整分页；此模式会启动 Chrome
.venv/bin/python linux_do_scraper.py --scrape --full --no-proxy
```

## 飞书日报

```bash
.venv/bin/python linux_do_scraper.py --scrape --rss --no-proxy \
  | .venv/bin/python push_to_feishu.py
```

详细配置参见 [README-飞书日报.md](README-%E9%A3%9E%E4%B9%A6%E6%97%A5%E6%8A%A5.md)。

## macOS 配置

默认不启用代理。需要代理时设置环境变量：

```bash
export LINUXDO_PROXY="127.0.0.1:7897"
```

可选路径变量：

| 变量 | 用途 |
|---|---|
| `LINUXDO_PROXY` | HTTP 代理地址 |
| `LINUXDO_CHROME_PATH` | 自定义 Chrome 可执行文件路径 |
| `LINUXDO_BROWSER_DATA` | 自定义 Chrome 登录资料目录 |
| `LINUXDO_DATA_DIR` | 自定义 macOS 应用数据目录 |

如果项目目录中已有 `browser_data/`，程序会优先沿用，避免丢失现有登录状态。

## 项目入口

| 文件 | 作用 |
|---|---|
| `run_mac.command` | macOS 双击启动入口 |
| `mac_setup.sh` | 创建虚拟环境并安装依赖 |
| `build_mac_app.sh` | 构建 `dist/Linuxdoday.app` |
| `install_mac_app.sh` | 安装并打开 Mac 应用 |
| `linux_do_gui.py` | macOS 图形界面 |
| `linux_do_scraper.py` | RSS/浏览器双通道帖子抓取 |
| `fetch_content.py` | 正文补全 |
| `push_to_feishu.py` | 飞书增量同步 |

## 注意事项

- 自动点赞和回复具有账号风险，默认保持关闭，建议以浏览和数据整理为主。
- 请合理控制运行频率并遵守 Linux.do 社区规则。
- 登录在 Chrome 中完成，项目不保存账号密码。
- 浏览器资料可能包含登录 Cookie，已通过 `.gitignore` 排除，请勿手工提交。

## License

MIT
