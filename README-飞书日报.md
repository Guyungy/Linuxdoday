# Linux.do 帖子数据抓取 → 飞书多维表格

改造目标：把 Linux.do 全板块帖子数据抓下来，写入飞书多维表格，为**日报仪表盘**提供数据源。

## 新增文件

| 文件 | 作用 |
|---|---|
| `linux_do_scraper.py` | 帖子抓取器（DrissionPage 真实浏览器，绕过 Cloudflare） |
| `push_to_feishu.py` | 把抓取结果增量写入飞书多维表格 |
| `data/linuxdo_topics.json` | 本地增量缓存（Topic ID 去重） |

## 工作流

```bash
# 1. 首次：打开浏览器人工登录（之后登录态存在 browser_data/ 复用）
python linux_do_scraper.py --browse

# 2. 抓取（默认近期模式：每板块前3页≈90条，全板块约1000+条）+ 写入飞书
python linux_do_scraper.py --scrape 2>/dev/null | python push_to_feishu.py

# 无浏览器轻量模式（每板块最新约25条，无浏览量/回复数）
python linux_do_scraper.py --scrape --rss 2>/dev/null | python push_to_feishu.py

# 3. 常用变体
python linux_do_scraper.py --scrape --cats 开发调优,前沿快讯   # 指定板块
python linux_do_scraper.py --scrape --full                     # 全量分页(大)
python linux_do_scraper.py --scrape --limit 10                 # 每板块限10条
python linux_do_scraper.py --scrape --no-proxy                 # 不走代理
python push_to_feishu.py --dry-run                             # 只看不写
```

## 关键技术点

1. **两种抓取通道**：`--rss` 通过 `curl_cffi` 读取板块 RSS，不启动浏览器，适合每日新帖增量。完整 Discourse JSON 仍受 Cloudflare 保护，默认模式使用真实 Chrome 中的同源 `fetch` 获取分页、浏览量和回复数。
2. **数据源**：Discourse 官方 JSON API（浏览器内 fetch），字段结构化：作者/回复数/浏览量/发布时间/最近活跃/分类。`/c/<slug>.json?page=N` 分页。
3. **增量去重**：本地 `data/linuxdo_topics.json` 按 Topic ID 缓存，只把新增帖子写入飞书。
4. **写入飞书**：`lark-cli base +record-batch-create`，每批 ≤200 条。表结构见下。

## 飞书多维表格

- Base：**Linux.do 帖子日报**（token `LYdZbR3DTaFPeYsHP8ScqPVCnFe`）
- 表：**帖子主题**
- 字段：标题(text) / 帖子链接(url) / 作者(text) / 板块(text) / 回复数(number) / 浏览量(number) / 发布时间(datetime) / 最近活跃(datetime) / Topic ID(text) / 标签(select) / 正文(text) / 摘要(text) / 入库时间(created_at)

## 定时运行（可选，日报数据源）

每天跑一次 `python linux_do_scraper.py --scrape 2>/dev/null | python push_to_feishu.py`，
即可让多维表格保持最新，供仪表盘展示"今日新增 / 热门话题 / 板块分布"。

## 注意事项

- 登录态保存在 `browser_data/`（含 cookie），别提交到 git
- 防风控：抓取带随机延迟，别频繁跑
- 板块配置与 `linux_do_gui.py` 一致；`--full` 会抓上万条，慎用
