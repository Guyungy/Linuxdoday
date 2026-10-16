set -euo pipefail
LIVE=/Users/a1/Code/Linuxdoday
BK="$1"
mkdir -p "$BK"
cd "$LIVE"
echo "== pre-deploy state =="
git rev-parse HEAD | tee "$BK/head.txt"
git status --porcelain=v1 | tee "$BK/status.txt"
for f in README.md fetch_content.py linux_do_scraper.py push_to_feishu.py service.py tests/test_core.py daily_report.py hot_topics.py report_status.py common.py; do
  cp -p "$f" "$BK/$(basename "$f")"
done
cp -Rp reports "$BK/reports"
echo "== backup =="
ls -la "$BK"
echo "== 工作区必须是干净的：脏了就停，不猜也不丢 =="
# 原写法是 `git checkout -- .`（注释写「content already committed in 6118cf4」）。
# 它在这里做两件坏事：一是把未提交的改动直接抹掉 —— 上面 9–11 行的备份只覆盖写死的
# 那几个文件，改了清单外的 tracked 文件就**没有备份**地没了；二是它恰好拆掉了 git 自己
# 的安全网 —— `git merge --ff-only` 本来会因为「会覆盖本地改动」而拒绝执行，被它清干净
# 后就照常快进了。脏工作区是「有人正在改东西」的信号，怎么处理该由人决定。
# untracked 不算脏（data/ 与 reports/ 下的产物常年在），故判据加 --untracked-files=no。
if [ -n "$(git status --porcelain=v1 --untracked-files=no)" ]; then
  echo "FATAL: 工作区有未提交的 tracked 改动，deploy 拒绝覆盖。请先 commit / stash 后重跑：" >&2
  git status --porcelain=v1 --untracked-files=no >&2
  exit 1
fi

echo "== fast-forward main -> delivered revision =="
# --- 落地目标：按 commit 钉死（POLL-77）---
# 原写法是分支名 agent/builder-a4/poll-69：live 目录里的 *本地* 同名分支指向 b45156a
# （返工前），与 GitHub 上同名分支 c45dd30 已分叉（ahead 1, behind 1）。
# 从 main(334699b) 执行 `git merge --ff-only agent/builder-a4/poll-69` 会 **成功** 快进到
# b45156a —— 即返工前的代码，而不是复验通过的 c45dd30。故改为按 commit sha 落地。
TARGET=c45dd30d9c3f6ab7b7e6ff1a522587dd10f0030c
git rev-parse --verify --quiet "${TARGET}^{commit}" >/dev/null \
  || { echo "FATAL: 目标 commit ${TARGET} 不在本仓库对象库中，部署中止" >&2; exit 1; }
git merge-base --is-ancestor HEAD "$TARGET" \
  || { echo "FATAL: ${TARGET} 不是 HEAD($(git rev-parse HEAD)) 的后代，--ff-only 落不了地，部署中止" >&2; exit 1; }
git merge --ff-only "$TARGET"
[ "$(git rev-parse HEAD)" = "$TARGET" ] \
  || { echo "FATAL: 落地后 HEAD=$(git rev-parse HEAD) != ${TARGET}，部署中止" >&2; exit 1; }
echo "== post-deploy state =="
git rev-parse HEAD
git status --porcelain=v1
echo "== verify report artifacts survived =="
cd "$LIVE"
for f in reports/*.md; do
  a=$(shasum -a 256 "$f" | cut -d' ' -f1); b=$(shasum -a 256 "$BK/$(basename "$f")" | cut -d' ' -f1)
  [ "$a" = "$b" ] && echo "OK  $f" || { echo "MISMATCH $f"; exit 1; }
done
