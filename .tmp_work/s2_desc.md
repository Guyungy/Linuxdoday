## 背景
POLL-78（轮次时限改墙钟口径，`604723b`，位于 `agent/builder-a4/poll-78`）已交付、POLL-108 判「有条件通过」。但存在 **落地形态违约**（POLL-107 上线前检查单「序 3」）：

- 分支 `agent/builder-a4/poll-78` 起自旧 main `334699b`，**未包含** 复验通过的返工版 `c45dd30`（POLL-76/POLL-69 返工）。
- 与 `c45dd30` 合入后存在**三处语义冲突**：timeout 收口 vs P1 `record_failure` 记账、scheduler、README。

## 客户已批准的处置方向（POLL-107「照你推荐」）
**先落 `c45dd30`，再把 `604723b` rebase 上去解三处冲突。**

## 前置依赖（本 issue 为 backlog，需先满足再置 todo）
1. `c45dd30` 已合入 main / live 已落到 `c45dd30`（POLL-117 只是 deploy.sh 清理，不等它；等**落地协调 issue** 把 `c45dd30` 落地）。
2. 落地协调 issue 明确告知 `c45dd30` 已就位、可 rebase。

在满足前置前**不要启动**；被 promote 为 todo 时才执行。

## 交付要求（DoD）
1. 把 `agent/builder-a4/poll-78`（`604723b`）rebase 到 `c45dd30` 之上，逐个解决三处语义冲突：
   - **timeout 收口 vs P1 `record_failure` 记账**：需与客户/协调确认「timeout 收口是否取代/兼容 P1 record_failure」后按裁决解算（POLL-107 相关裁决若无明确结论，先把方案写评论请示，勿擅自吞掉任一语义）。
   - **scheduler** 相关改动合并。
   - **README** 表述合并。
2. rebase 后 `bash -n` / 相关单测通过；确认 `608 something` 语义与 POLL-78 验收口径一致（墙钟口径、睡眠冻结修复不回归）。
3. 推送分支、回报新 commit sha 与三处冲突各自的解算说明（每个冲突：取舍理由）。

## 验收视角
accepter 复核：`604723b` 的改动已在 `c45dd30` 基底上干净存在；三处冲突均有明确取舍且不丢失任何一方语义；墙钟口径行为经单测复验。
