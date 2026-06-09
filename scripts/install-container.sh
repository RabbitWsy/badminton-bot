#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-}"
PLAYWRIGHT_INSTALL_DEPS="${PLAYWRIGHT_INSTALL_DEPS:-auto}"

case "$PLAYWRIGHT_INSTALL_DEPS" in
  auto|true|false) ;;
  *)
    echo "PLAYWRIGHT_INSTALL_DEPS 只能是 auto、true 或 false，当前值: $PLAYWRIGHT_INSTALL_DEPS"
    exit 2
    ;;
esac

run_as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
    return
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "需要安装系统依赖，但当前用户不是 root，且未找到 sudo。"
    echo "请用 root 运行本脚本、安装 sudo，或设置 PLAYWRIGHT_INSTALL_DEPS=false 跳过系统依赖。"
    exit 2
  fi

  sudo "$@"
}

ensure_root_access() {
  if [ "$(id -u)" -eq 0 ]; then
    return
  fi

  if ! command -v sudo >/dev/null 2>&1; then
    echo "需要安装系统依赖，但当前用户不是 root，且未找到 sudo。"
    echo "请用 root 运行本脚本、安装 sudo，或设置 PLAYWRIGHT_INSTALL_DEPS=false 跳过系统依赖。"
    exit 2
  fi

  echo "即将通过 sudo 安装 Chromium 系统依赖和 xvfb，可能会提示输入当前用户密码。"
  sudo -v
}

create_venv() {
  if command -v uv >/dev/null 2>&1; then
    if [ -n "$PYTHON_BIN" ]; then
      uv venv --clear --python "$PYTHON_BIN" .venv
    else
      uv venv --clear .venv
    fi
    return
  fi

  if ! "${PYTHON_BIN:-python3}" -m venv --clear .venv; then
    echo "无法创建 .venv。请安装 uv，或在 Debian/Ubuntu 上安装 python3-venv 后重试。"
    exit 2
  fi
}

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

if [ ! -x ".venv/bin/python" ]; then
  create_venv
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python -r requirements.txt
else
  if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
    if ! .venv/bin/python -m ensurepip --upgrade; then
      echo "当前 .venv 没有 pip，且 ensurepip 不可用。请安装 uv，或安装 python3-venv 后删除 .venv 重试。"
      exit 2
    fi
  fi
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

if [ "$PLAYWRIGHT_INSTALL_DEPS" = "true" ] || [ "$PLAYWRIGHT_INSTALL_DEPS" = "auto" ]; then
  ensure_root_access
  run_as_root .venv/bin/python -m playwright install-deps chromium
  .venv/bin/python -m playwright install chromium
  if command -v apt-get >/dev/null 2>&1; then
    run_as_root apt-get update
    run_as_root apt-get install -y xvfb
  elif ! command -v xvfb-run >/dev/null 2>&1; then
    echo "未找到 apt-get，无法自动安装 xvfb。请手动安装 xvfb，或提供 DISPLAY。"
    exit 2
  fi
else
  .venv/bin/python -m playwright install chromium
fi

echo "容器依赖安装完成。"
echo "测试运行：FUDAN_WAIT_UNTIL_OPEN=false bash scripts/run-once.sh --dry-run"
echo "每日前台循环：bash scripts/run-daily.sh"
