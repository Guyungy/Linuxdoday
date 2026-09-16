#!/bin/zsh
set -euo pipefail

SCRIPT_DIR=${0:A:h}
cd "$SCRIPT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  print "首次运行，正在创建 macOS 虚拟环境…"
  ./mac_setup.sh
fi

exec .venv/bin/python linux_do_gui.py
