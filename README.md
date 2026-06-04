# 自动准时抢羽毛球场地

每天早上 7 点开放羽毛球场预约。本项目用 Playwright 直接打开江湾羽毛球场预约页，必要时自动跳转到复旦统一认证登录，登录后回到预约页并按优先级尝试预约。

直达预约页：

```text
https://booking.fudan.edu.cn/reservation/fe/site/reservationInfo?id=1055
```

未登录时会跳到用户名密码认证页面；脚本每天运行时都会重新登录。

## 当前策略

- 预约目标：默认今天往后第 2 天，也就是学校页面实际开放的最远日期。
- 场馆：`江湾综合体育馆-羽毛球`。
- 时段：默认只抢 15:00 及以后。
- 优先级：`21:00-22:30`、`20:00-21:00`、`19:00-20:00`、`18:00-19:00`、`17:00-18:00`、`16:00-17:00`、`15:00-16:00`。
- 数量：最多 3 个时段；每次只提交 1 个时段，成功后重新打开预约页再继续下一段。
- 执行方式：容器常驻进程默认每天 `06:58:30` 启动一次任务，提前登录并进入预约页，等到 `07:00:00` 后刷新预约页并开始点击。
- 页面弹出「阅读须知」时，会先点击「确定」再继续预约。
- 点击时段格子后出现的「已预约/点击查看详情」浮层不会被当作成功；只有跳转后的预约记录里出现目标日期、目标时段、「已预约」和「待签到」才算预约成功。

## 容器部署

服务器没有 systemd 也没关系。进入容器后，在项目目录执行：

```bash
cp .env.example .env
nano .env
bash scripts/install-container.sh
```

`.env` 至少要填写：

```bash
FUDAN_USERNAME=你的学号或账号
FUDAN_PASSWORD=你的密码
FUDAN_PHONE=你的手机号
```

安装脚本默认会安装 Python 依赖、Playwright Chromium、Chromium 系统依赖和 `xvfb`。其中系统依赖需要管理员权限：

- root 用户会直接安装。
- 非 root 用户会通过 `sudo` 安装，并在终端提示输入当前用户密码。

如果容器镜像已经内置 Chromium 系统依赖和 `xvfb`，可以跳过系统依赖安装：

```bash
PLAYWRIGHT_INSTALL_DEPS=false bash scripts/install-container.sh
```

非 Debian/Ubuntu 系镜像如果没有 `apt-get`，脚本无法自动安装 `xvfb`，需要在镜像里提前安装，或提供可用的 `DISPLAY`。

## 每天自动运行

容器里没有 systemctl，所以用一个前台循环进程：

```bash
bash scripts/run-daily.sh
```

这个脚本会读取 `.env`，默认每天 `06:58:30` 运行一次 `badminton_bot.py`。`booking.fudan.edu.cn` 目前会拦截 headless 浏览器，所以默认使用 headed Chrome；容器没有 `DISPLAY` 时，`run-daily.sh` 会自动通过 `xvfb-run` 提供虚拟显示。可以在 `.env` 里调整：

```bash
FUDAN_CONTAINER_START_TIME=06:58:30
FUDAN_OPEN_TIME=07:00:00
FUDAN_SUBMIT_RESULT_TIMEOUT_MS=2000
FUDAN_HEADLESS=false
FUDAN_USE_CDP_BROWSER=true
```

实际部署时，让容器的主进程执行 `bash scripts/run-daily.sh`，或者在 `tmux`/`screen`/`nohup` 中运行它。

## 手动测试

不提交预约，只验证登录、进入预约页、定位日期和时段：

```bash
FUDAN_WAIT_UNTIL_OPEN=false bash scripts/run-once.sh --dry-run
```

只登录并进入预约页：

```bash
bash scripts/run-once.sh --login-only
```

指定日期测试：

```bash
FUDAN_WAIT_UNTIL_OPEN=false bash scripts/run-once.sh --target-date 2026-04-29 --dry-run
```

本地 Mac 默认会启动系统 Chrome 并通过 CDP 连接控制。也可以显式指定：

```bash
FUDAN_USE_CDP_BROWSER=true FUDAN_WAIT_UNTIL_OPEN=false bash scripts/run-once.sh --dry-run
```

## 配置

核心配置在 `.env`：

```bash
FUDAN_VENUE_URL=https://booking.fudan.edu.cn/reservation/fe/site/reservationInfo?id=1055
FUDAN_TARGET_DAYS_AHEAD=2
FUDAN_MAX_SLOTS=3
FUDAN_MIN_START_HOUR=15
FUDAN_PREFERRED_SLOTS=21:00-22:30,20:00-21:00,19:00-20:00,18:00-19:00,17:00-18:00,16:00-17:00,15:00-16:00
```

预约成功后，脚本会重新打开 `FUDAN_VENUE_URL`，继续尝试下一个时段。

脚本不再加载或保存 `storage_state.json`，每次运行都会重新登录；同一轮抢场里的刷新仍然使用当前浏览器会话。

## 项目文件

- `badminton_bot.py`：预约脚本。
- `.env.example`：配置模板，真实账号密码放在 `.env`。
- `scripts/install-container.sh`：容器内安装依赖。
- `scripts/run-once.sh`：单次运行，会在无 `DISPLAY` 且 `FUDAN_HEADLESS=false` 时自动使用 `xvfb-run`。
- `scripts/run-daily.sh`：无 systemd 环境下的每日前台循环。
- `logs/`：运行截图和失败截图。
