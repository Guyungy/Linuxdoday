#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
  print -u2 "此安装脚本仅支持 macOS。"
  exit 1
fi

if [[ ! -x "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" && \
      ! -x "$HOME/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" ]]; then
  print -u2 "未找到 Google Chrome，请先安装：https://www.google.com/chrome/"
  exit 1
fi

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt

print ""
print "安装完成。"
print "无浏览器抓取：.venv/bin/python linux_do_scraper.py --scrape --rss --no-proxy"
print "启动图形界面：双击 run_mac.command"
