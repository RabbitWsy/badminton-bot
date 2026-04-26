#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "已创建 .env，请填写账号、密码和手机号后重新运行本脚本：${ROOT_DIR}/.env"
  exit 2
fi

missing_keys=()
for key in FUDAN_USERNAME FUDAN_PASSWORD FUDAN_PHONE FUDAN_VENUE_URL; do
  if ! grep -Eq "^${key}=.+" .env; then
    missing_keys+=("$key")
  fi
done
if [ "${#missing_keys[@]}" -gt 0 ]; then
  echo ".env 缺少必填项：${missing_keys[*]}"
  echo "请填写后重新运行：bash scripts/install-container.sh"
  exit 2
fi

if [ ! -d ".venv" ]; then
  "${PYTHON_BIN}" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

if [ "${PLAYWRIGHT_INSTALL_DEPS:-auto}" = "true" ] || {
  [ "${PLAYWRIGHT_INSTALL_DEPS:-auto}" = "auto" ] && [ "$(id -u)" -eq 0 ];
}; then
  .venv/bin/python -m playwright install --with-deps chromium
else
  .venv/bin/python -m playwright install chromium
fi

echo "容器依赖安装完成。"
echo "测试运行：FUDAN_WAIT_UNTIL_OPEN=false .venv/bin/python badminton_bot.py --dry-run"
echo "每日前台循环：bash scripts/run-daily.sh"
