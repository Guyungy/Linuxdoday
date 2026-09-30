set -euo pipefail
LIVE=/Users/a1/Code/Linuxdoday
BK="$1"
mkdir -p "$BK"
cd "$LIVE"
echo "== pre-deploy state =="
git rev-parse HEAD | tee "$BK/head.txt"
git status --porcelain=v1 | tee "$BK/status.txt"
for f in README.md fetch_content.py linux_do_scraper.py push_to_feishu.py service.py tests/test_core.py daily_report.py hot_topics.py; do
  cp -p "$f" "$BK/$(basename "$f")"
done
cp -Rp reports "$BK/reports"
echo "== backup =="
ls -la "$BK"
echo "== discard local mods (content already committed in 6118cf4) =="
git checkout -- .
echo "== drop untracked colliders (backed up) =="
rm -f daily_report.py hot_topics.py
rm -rf reports
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
