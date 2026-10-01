## 背景
POLL-77（deploy.sh 落地目标改为按 commit 钉死）已交付、`1d8c500` 位于 `agent/builder-a4/poll-77`。但其落地仍被一处闸门阻塞（POLL-107 上线前检查单「序 2」）：deploy.sh 第 **17–19 行**存在与被 git 跟踪文件冲突的清理逻辑。

## 问题定位（已核实，deploy.sh 当前内容）
- 第 17 行 `echo "== drop untracked colliders (backed up) =="`
- 第 18 行 `rm -f daily_report.py hot_topics.py`
- 第 19 行 `rm -rf reports`

第 18–19 行会删除 **已被 git 跟踪** 的文件 `daily_report.py`、`hot_topics.py`（见仓库根目录，均 tracked），`reports/` 目录亦被 git 跟踪；删除后第 39 行 `for f in reports/*.md` 的校验 glob 落空、`set -euo pipefail` 下部署会在校验前就已把跟踪文件从工作区删掉。真跑 deploy 前必须处置。

## 客户已批准的处置方向（POLL-107「照你推荐」）
**最小改法：删掉第 17–19 行这三行**——报告清理不再放进 deploy 流程，改由手动/独立脚本处理，不进 deploy.sh。

## 交付要求（DoD）
1. 在 `agent/builder-a4/poll-77` 分支上基于 `1d8c500` 新增一个 commit：从 deploy.sh 删除第 17–19 行（危险清理 + glob 落空说明一并移除，若脚本其余部分依赖 `reports` 存在请同步核对）。
2. 保留 POLL-77 已钉死的目标 sha（`c45dd30d9c3f6ab7b7e6ff1a522587dd10f0030c`）逻辑，不回归「按 commit 落地」修复。
3. 本地验证：`bash -n deploy.sh` 语法通过；对删除后剩余逻辑做一眼核对（确认没有其它 `rm` 依赖被删的 tracked 文件、校验 glob 不再落空）。
4. 推送分支到远端（key 含 POLL-107 使 PR 关联），并回报新 commit sha 与改动 diff 摘要。
5. 若非最小改法能达成而是需要额外改动，先把方案写在 issue 评论再动手，不要擅自扩大范围。

## 验收视角
accepter 复核：deploy.sh 中不再存在会删除 tracked 文件/使 glob 落空的 `rm -f daily_report.py / rm -rf reports`；钉死 sha 逻辑仍在；`bash -n` 通过。
