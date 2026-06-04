#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -x ".venv/bin/python" ]; then
  echo "没有找到 .venv/bin/python。请先运行：bash scripts/install-container.sh"
  exit 2
fi

use_xvfb="$(
  .venv/bin/python - <<'PY'
import os
import sys

from dotenv import load_dotenv

load_dotenv(".env")
headless = os.getenv("FUDAN_HEADLESS", "false").strip().lower()
needs_display = sys.platform.startswith("linux") and not os.getenv("DISPLAY")
print("1" if headless in {"0", "false", "no", "off"} and needs_display else "0")
PY
)"

if [ "$use_xvfb" = "1" ]; then
  if ! command -v xvfb-run >/dev/null 2>&1; then
    echo "FUDAN_HEADLESS=false 且当前没有 DISPLAY，但未找到 xvfb-run。请安装 xvfb 后再运行。"
    exit 2
  fi
  exec xvfb-run -a -s "-screen 0 1440x1100x24" .venv/bin/python badminton_bot.py "$@"
fi

exec .venv/bin/python badminton_bot.py "$@"
