#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -x ".venv/bin/python" ]; then
  echo "没有找到 .venv/bin/python。请先运行：bash scripts/install-container.sh"
  exit 2
fi

run_bot() {
  local use_xvfb
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
      return 2
    fi
    xvfb-run -a -s "-screen 0 1440x1100x24" .venv/bin/python badminton_bot.py
  else
    .venv/bin/python badminton_bot.py
  fi
}

while true; do
  next_run="$(
    .venv/bin/python - <<'PY'
from datetime import datetime, timedelta
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv(".env")
tz = ZoneInfo(os.getenv("FUDAN_TIMEZONE", "Asia/Shanghai"))
run_time = os.getenv("FUDAN_CONTAINER_START_TIME", os.getenv("FUDAN_TIMER_TIME", "06:58:30"))
parts = [int(part) for part in run_time.split(":")]
if len(parts) == 2:
    parts.append(0)

now = datetime.now(tz)
target = now.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)
if target <= now:
    target += timedelta(days=1)
print(f"{int(target.timestamp())}|{target.strftime('%Y-%m-%d %H:%M:%S %Z')}")
PY
  )"
  IFS="|" read -r next_epoch next_display <<< "$next_run"
  now_epoch="$(date +%s)"
  sleep_seconds=$((next_epoch - now_epoch))
  if [ "$sleep_seconds" -gt 0 ]; then
    echo "下一次运行时间：${next_display}，等待 ${sleep_seconds}s"
    sleep "$sleep_seconds"
  fi

  echo "开始运行：$(date '+%Y-%m-%d %H:%M:%S %Z')"
  if ! run_bot; then
    echo "本次运行失败，等待下一轮。"
  fi
  sleep 60
done
