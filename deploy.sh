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
git merge --ff-only agent/builder-a4/poll-69
echo "== post-deploy state =="
git rev-parse HEAD
git status --porcelain=v1
echo "== verify report artifacts survived =="
cd "$LIVE"
for f in reports/*.md; do
  a=$(shasum -a 256 "$f" | cut -d' ' -f1); b=$(shasum -a 256 "$BK/$(basename "$f")" | cut -d' ' -f1)
  [ "$a" = "$b" ] && echo "OK  $f" || { echo "MISMATCH $f"; exit 1; }
done
