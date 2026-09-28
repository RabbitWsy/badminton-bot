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
- 表格和跨周切换最多等待 15 秒，加载完成立即继续；提交后最多等待 10 秒核实预约记录。默认在开放后 90 秒内开始重试，最后一次提交和核实可能晚于该时刻完成。
- 每一轮都严格按 `FUDAN_PREFERRED_SLOTS` 的填写顺序检查，不按时间或历史尝试次数重新排序；不可约的跳过，明确失败后继续下一项。下一轮仍从列表首项开始，排除已经成功、冲突或结果待核实的时段。
- 例如配置 `21:00-22:30,20:00-21:00,19:00-20:00`，三项都有余量时依次提交；第一项约满时从第二项开始。每项每轮最多提交一次，避免某项失败耗尽整个窗口。
- 时段重叠则跳过，账号预约额度达到上限则停止。结果不明的提交暂占一个名额、本轮不重复提交，并在结束前通过「我的预约」核实。
- 网站明确提示「前方拥挤请稍后再试」时，单独记为 `busy`，立即结束结果等待，不占预约名额。先按约 1、2、4、8 秒退避，再重新读取余量、按原优先级尝试；仍受本轮截止时间约束。普通网络超时或「请求处理中」仍属结果不明，不直接重发。
- 每轮开始时记录实际 `FUDAN_MAX_SLOTS`、时段顺序和剩余提交窗口。若只预约一场，设置 `FUDAN_MAX_SLOTS=1`；只有成功或真正结果不明的提交才占用名额，明确失败和拥挤不会消耗名额。
- 日历使用日期列和时段行直接定位，批量读取候选状态，点击前再次检查。抢场期间不等待网络空闲，也不固定等待不存在的确认弹窗；成功截图移至本轮结束，提交结果一确认就继续下一项。
- 刷新中 iframe 被移除、替换或执行上下文失效时，重新获取当前页面后继续读取；不会把这种暂时失效当作约满。若异常发生在可能已经提交之后，则核对预约记录，不直接重发。
- 时段、提交按钮及确认弹窗使用浏览器鼠标点击，先等待控件可见、稳定且未被遮挡。鼠标按下后默认 60 毫秒释放，不使用 JavaScript 直接触发点击。
- 无进展时按约 1、2、4、8 秒逐步降低检查频率，并增加 0–0.3 秒抖动。所有剩余目标时段都约满时，分别等待约 3、6 秒复查，连续 3 轮仍满就结束；这是无可用场次，并非程序异常。首次检查及顺序尝试可用时段不受这段退避影响。

可通过 `FUDAN_RETRY_MAX_INTERVAL_SECONDS`、`FUDAN_RETRY_JITTER_SECONDS`、`FUDAN_FULL_SLOT_CHECKS` 和 `FUDAN_CLICK_DELAY_MS` 调整上述行为。将 `FUDAN_FULL_SLOT_CHECKS=0` 可保留整个重试窗口内的余量检查。降低请求频率不能保证站点不会触发风控。

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
FUDAN_SUBMIT_RESULT_TIMEOUT_MS=10000
FUDAN_BOOKING_READY_TIMEOUT_MS=15000
FUDAN_RETRY_UNTIL_SECONDS=90
FUDAN_HEADLESS=false
FUDAN_USE_CDP_BROWSER=true
FUDAN_CDP_STARTUP_ATTEMPTS=3
FUDAN_CDP_STARTUP_TIMEOUT_SECONDS=30
```

实际部署时，让容器的主进程执行 `bash scripts/run-daily.sh`，或者在 `tmux`/`screen`/`nohup` 中运行它。

Headed Chromium 启动失败时，脚本会自动重试并把浏览器 stderr 写到 `logs/*-chromium-attempt-*.log`，方便排查 Xvfb、系统依赖或浏览器包问题。

每轮业务日志写入 `logs/*-run.log`，包括目标日期、加载耗时和提交反馈。退出码 `0` 表示已有确认预约、连续约满后正常结束，或 dry-run 正常结束，`2` 表示未确认到预约，`3` 表示仍有提交结果待核实（可能同时有已确认预约）。`no-slot-booked` 截图文件名不代表服务器一定没有生成预约，判断实际结果应以「我的预约」为准。

预约系统可能返回「有效期内爽约次数与已预约未开始时段数量合计达到上限（3）」。这是账号额度限制，并非每天一定能新增 3 个时段；脚本遇到该反馈会停止提交。

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

常规运行到 `FUDAN_OPEN_TIME + FUDAN_RETRY_UNTIL_SECONDS` 后不再开始新的提交，已发出的提交仍等待核实。设置 `FUDAN_WAIT_UNTIL_OPEN=false` 时，重试窗口从本轮开始计算；`--dry-run` 只按顺序检查一轮。

## 项目文件

- `badminton_bot.py`：预约脚本。
- `calendar_dom.js`：日历 DOM 定位和状态读取，部署时需要与 Python 脚本一起复制。
- `.env.example`：配置模板，真实账号密码放在 `.env`。
- `scripts/install-container.sh`：容器内安装依赖。
- `scripts/run-once.sh`：单次运行，会在无 `DISPLAY` 且 `FUDAN_HEADLESS=false` 时自动使用 `xvfb-run`。
- `scripts/run-daily.sh`：无 systemd 环境下的每日前台循环。
- `logs/`：每轮业务日志、浏览器启动日志和截图。

## 回归验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用本地模拟页面，不登录、不提交真实预约。实站检查使用上面的 `--dry-run`。
