from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_PREFERRED_SLOTS = [
    "21:00-22:30",
    "20:00-21:00",
    "19:00-20:00",
    "18:00-19:00",
    "17:00-18:00",
    "16:00-17:00",
    "15:00-16:00",
]

DEFAULT_VENUE_URL = "https://booking.fudan.edu.cn/reservation/fe/site/reservationInfo?id=1055"
CALENDAR_SCRIPT = Path(__file__).with_name("calendar_dom.js").read_text(encoding="utf-8")


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    phone: str
    venue_keyword: str
    venue_url: str
    target_days_ahead: int
    max_slots: int
    min_start_hour: int
    preferred_slots: list[str]
    timezone: str
    open_time: str
    wait_until_open: bool
    retry_until_seconds: float
    retry_interval_seconds: float
    page_settle_timeout_ms: int
    submit_result_timeout_ms: int
    headless: bool
    use_cdp_browser: bool
    slow_mo_ms: int
    browser_channel: str
    browser_executable_path: str
    log_dir: Path
    cdp_startup_attempts: int = 3
    cdp_startup_timeout_seconds: float = 30
    cdp_retry_delay_seconds: float = 1
    navigation_timeout_ms: int = 30000
    action_timeout_ms: int = 5000
    booking_ready_timeout_ms: int = 15000
    retry_max_interval_seconds: float = 8
    retry_jitter_seconds: float = 0.3
    full_slot_checks: int = 3
    click_delay_ms: int = 60


@dataclass(frozen=True)
class SubmitResult:
    status: str
    feedback: str = ""


class BotError(RuntimeError):
    pass


class CalendarNotReady(BotError):
    pass


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_slots(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_PREFERRED_SLOTS.copy()
    return list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def slot_start_hour(slot: str) -> int:
    match = re.match(r"\s*(\d{1,2}):", slot)
    if not match:
        raise ValueError(f"无法解析时段开始时间: {slot}")
    return int(match.group(1))


def load_config(env_file: Path) -> Config:
    if env_file.exists():
        load_dotenv(env_file)

    root = Path.cwd()
    min_start_hour = int(os.getenv("FUDAN_MIN_START_HOUR", "15"))
    preferred_slots = [
        slot for slot in parse_slots(os.getenv("FUDAN_PREFERRED_SLOTS"))
        if slot_start_hour(slot) >= min_start_hour
    ]
    if not preferred_slots:
        raise BotError("FUDAN_PREFERRED_SLOTS 里没有符合 FUDAN_MIN_START_HOUR 的时段。")

    return Config(
        username=os.getenv("FUDAN_USERNAME", "").strip(),
        password=os.getenv("FUDAN_PASSWORD", ""),
        phone=os.getenv("FUDAN_PHONE", "").strip(),
        venue_keyword=os.getenv("FUDAN_VENUE_KEYWORD", "江湾综合体育馆-羽毛球").strip(),
        venue_url=os.getenv("FUDAN_VENUE_URL", DEFAULT_VENUE_URL).strip() or DEFAULT_VENUE_URL,
        target_days_ahead=int(os.getenv("FUDAN_TARGET_DAYS_AHEAD", "2")),
        max_slots=int(os.getenv("FUDAN_MAX_SLOTS", "3")),
        min_start_hour=min_start_hour,
        preferred_slots=preferred_slots,
        timezone=os.getenv("FUDAN_TIMEZONE", "Asia/Shanghai").strip(),
        open_time=os.getenv("FUDAN_OPEN_TIME", "07:00:00").strip(),
        wait_until_open=parse_bool(os.getenv("FUDAN_WAIT_UNTIL_OPEN"), True),
        retry_until_seconds=float(os.getenv("FUDAN_RETRY_UNTIL_SECONDS", "90")),
        retry_interval_seconds=float(os.getenv("FUDAN_RETRY_INTERVAL_SECONDS", "1")),
        page_settle_timeout_ms=int(os.getenv("FUDAN_PAGE_SETTLE_TIMEOUT_MS", "1200")),
        submit_result_timeout_ms=int(os.getenv("FUDAN_SUBMIT_RESULT_TIMEOUT_MS", "10000")),
        headless=parse_bool(os.getenv("FUDAN_HEADLESS"), False),
        use_cdp_browser=parse_bool(
            os.getenv("FUDAN_USE_CDP_BROWSER"),
            not parse_bool(os.getenv("FUDAN_HEADLESS"), False),
        ),
        slow_mo_ms=int(os.getenv("FUDAN_SLOW_MO_MS", "0")),
        browser_channel=os.getenv("FUDAN_BROWSER_CHANNEL", "").strip(),
        browser_executable_path=os.getenv("FUDAN_BROWSER_EXECUTABLE_PATH", "").strip(),
        log_dir=root / os.getenv("FUDAN_LOG_DIR", "logs"),
        cdp_startup_attempts=int(os.getenv("FUDAN_CDP_STARTUP_ATTEMPTS", "3")),
        cdp_startup_timeout_seconds=float(os.getenv("FUDAN_CDP_STARTUP_TIMEOUT_SECONDS", "30")),
        cdp_retry_delay_seconds=float(os.getenv("FUDAN_CDP_RETRY_DELAY_SECONDS", "1")),
        booking_ready_timeout_ms=int(os.getenv("FUDAN_BOOKING_READY_TIMEOUT_MS", "15000")),
        retry_max_interval_seconds=float(os.getenv("FUDAN_RETRY_MAX_INTERVAL_SECONDS", "8")),
        retry_jitter_seconds=float(os.getenv("FUDAN_RETRY_JITTER_SECONDS", "0.3")),
        full_slot_checks=int(os.getenv("FUDAN_FULL_SLOT_CHECKS", "3")),
        click_delay_ms=int(os.getenv("FUDAN_CLICK_DELAY_MS", "60")),
    )


class FudanBadmintonBot:
    def __init__(self, config: Config, *, dry_run: bool, target_date: str | None) -> None:
        self.config = config
        self.dry_run = dry_run
        self.tz = ZoneInfo(config.timezone)
        self.target_date = target_date or (
            datetime.now(self.tz).date() + timedelta(days=config.target_days_ahead)
        ).isoformat()
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.page: Any | None = None
        self.pending_slots: set[str] = set()
        self.all_slots_full = False
        self.run_log_path = self.config.log_dir / (
            datetime.now(self.tz).strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}-run.log"
        )

    def run_once(self, *, login_only: bool = False) -> int:
        with sync_playwright() as playwright:
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": 1440, "height": 1100},
                "locale": "zh-CN",
                "timezone_id": self.config.timezone,
            }
            browser_process: subprocess.Popen[Any] | None = None
            profile_dir: tempfile.TemporaryDirectory[str] | None = None
            if self.config.use_cdp_browser and not self.config.headless:
                browser, context, browser_process, profile_dir = self.launch_external_cdp_browser(
                    playwright,
                    context_kwargs,
                )
            else:
                launch_kwargs: dict[str, Any] = {
                    "headless": self.config.headless,
                    "slow_mo": self.config.slow_mo_ms,
                }
                if self.config.browser_channel:
                    launch_kwargs["channel"] = self.config.browser_channel
                if self.config.browser_executable_path:
                    launch_kwargs["executable_path"] = self.config.browser_executable_path
                browser = playwright.chromium.launch(**launch_kwargs)
                context = browser.new_context(**context_kwargs)
            context.set_default_timeout(self.config.action_timeout_ms)
            context.set_default_navigation_timeout(self.config.navigation_timeout_ms)
            self.page = context.new_page()

            try:
                self.open_venue()
                self.login_if_needed()
                if login_only:
                    self.log("登录流程完成。")
                    return 0

                if not self.is_booking_page():
                    self.open_venue()
                if self.config.wait_until_open:
                    self.wait_until_open_time()
                    self.refresh_booking_page()

                booked = self.book_with_retries()
                if booked:
                    self.log(f"已确认预约: {', '.join(booked)}")
                if self.pending_slots:
                    self.log(f"以下提交结果仍待核实，请检查我的预约: {', '.join(sorted(self.pending_slots))}")
                    self.save_screenshot("unconfirmed-booking")
                    return 3
                if booked:
                    self.save_screenshot("booked-summary")
                    self.log(f"完成预约: {', '.join(booked)}")
                    return 0

                if self.dry_run:
                    self.save_screenshot("dry-run")
                    self.log("dry-run 完成，没有提交预约。")
                    return 0

                if self.all_slots_full:
                    self.save_screenshot("all-slots-full")
                    self.log("目标时段已全部约满，任务正常结束，未新增预约。")
                    return 0

                self.save_screenshot("no-slot-booked")
                self.log("本轮没有确认到成功预约的时段。")
                return 2
            except Exception as exc:
                self.save_screenshot("error")
                self.log(f"失败: {exc}")
                raise
            finally:
                try:
                    context.close()
                except Exception as exc:
                    self.log(f"关闭浏览器 context 失败: {exc}")
                try:
                    browser.close()
                except Exception as exc:
                    self.log(f"关闭浏览器失败: {exc}")
                if browser_process:
                    self.terminate_browser_process(browser_process)
                if profile_dir:
                    self.cleanup_profile_dir(profile_dir)

    def launch_external_cdp_browser(
        self,
        playwright: Any,
        context_kwargs: dict[str, Any],
    ) -> tuple[Any, Any, subprocess.Popen[Any], tempfile.TemporaryDirectory[str]]:
        executable = self.find_browser_executable(playwright)
        attempts = max(1, self.config.cdp_startup_attempts)
        timeout_seconds = max(1.0, self.config.cdp_startup_timeout_seconds)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            profile_dir = tempfile.TemporaryDirectory(prefix="fudan-booking-profile-")
            port = self.free_port()
            stderr_path = self.browser_stderr_path(attempt)
            args = [
                executable,
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile_dir.name}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1440,1100",
                "about:blank",
            ]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                args.append("--no-sandbox")

            popen_kwargs: dict[str, Any] = {}
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True

            with stderr_path.open("wb") as stderr_file:
                process = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    **popen_kwargs,
                )

            endpoint = f"http://127.0.0.1:{port}"
            try:
                self.log(f"启动外部浏览器: 第 {attempt}/{attempts} 次，CDP 端口 {port}")
                ws_endpoint = self.wait_for_cdp_websocket(
                    endpoint,
                    process=process,
                    timeout_seconds=timeout_seconds,
                )
                browser = playwright.chromium.connect_over_cdp(ws_endpoint, timeout=10000)
                try:
                    context = browser.new_context(**context_kwargs)
                except Exception:
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                self.log(f"已通过外部浏览器连接: {executable}")
                return browser, context, process, profile_dir
            except Exception as exc:
                last_error = exc
                self.terminate_browser_process(process)
                self.log_browser_stderr_tail(stderr_path)
                self.cleanup_profile_dir(profile_dir)
                if attempt < attempts:
                    self.log(
                        f"外部浏览器启动失败: {exc}；"
                        f"{self.config.cdp_retry_delay_seconds:g} 秒后重试。"
                    )
                    time.sleep(max(0, self.config.cdp_retry_delay_seconds))

        raise BotError(f"外部浏览器启动失败，已重试 {attempts} 次: {last_error}")

    def wait_for_cdp_websocket(
        self,
        endpoint: str,
        *,
        process: subprocess.Popen[Any],
        timeout_seconds: float,
    ) -> str:
        deadline = time.monotonic() + timeout_seconds
        version_url = f"{endpoint}/json/version"
        last_error: Exception | str | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BotError(f"外部浏览器提前退出，退出码: {process.returncode}")
            try:
                with urllib.request.urlopen(version_url, timeout=0.5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                ws_endpoint = payload.get("webSocketDebuggerUrl")
                if isinstance(ws_endpoint, str) and ws_endpoint.startswith("ws"):
                    return ws_endpoint
                last_error = "DevTools JSON 中没有 webSocketDebuggerUrl"
            except Exception as exc:
                last_error = exc
            time.sleep(0.2)
        raise BotError(f"等待外部浏览器 CDP 端点超时: {last_error}")

    def terminate_browser_process(self, process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        self.signal_browser_process(process, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.signal_browser_process(process, signal.SIGKILL)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    def signal_browser_process(self, process: subprocess.Popen[Any], sig: signal.Signals) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except OSError:
            pass

    def cleanup_profile_dir(self, profile_dir: tempfile.TemporaryDirectory[str]) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                profile_dir.cleanup()
                return
            except Exception as exc:
                last_error = exc
                if attempt < 3:
                    time.sleep(0.2 * attempt)

        path = Path(profile_dir.name)
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception as exc:
            last_error = exc
        if path.exists():
            self.log(f"临时浏览器 profile 清理失败: {last_error}")

    def browser_stderr_path(self, attempt: int) -> Path:
        stamp = datetime.now(self.tz).strftime("%Y%m%d-%H%M%S")
        return self.config.log_dir / f"{stamp}-chromium-attempt-{attempt}.log"

    def log_browser_stderr_tail(self, path: Path, *, max_chars: int = 3000) -> None:
        try:
            if not path.exists() or path.stat().st_size == 0:
                return
            with path.open("rb") as file:
                size = path.stat().st_size
                file.seek(max(0, size - max_chars))
                text = file.read().decode("utf-8", errors="replace").strip()
        except Exception as exc:
            self.log(f"读取外部浏览器启动日志失败: {exc}")
            return

        if text:
            self.log(f"外部浏览器启动日志: {path}\n{text}")

    def find_browser_executable(self, playwright: Any) -> str:
        if self.config.browser_executable_path:
            return self.config.browser_executable_path

        candidates: list[str] = []
        if sys.platform == "darwin":
            candidates.extend([
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ])
        candidates.extend([
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ])

        for candidate in candidates:
            if "/" in candidate and Path(candidate).exists():
                return candidate
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return playwright.chromium.executable_path

    def free_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def log(self, message: str) -> None:
        stamp = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{stamp}] {message}"
        print(line, flush=True)
        with self.run_log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(line + "\n")

    def save_screenshot(self, label: str) -> None:
        if not self.page:
            return
        stamp = datetime.now(self.tz).strftime("%Y%m%d-%H%M%S")
        path = self.config.log_dir / f"{stamp}-{label}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            self.log(f"已保存截图: {path}")
        except Exception as exc:
            self.log(f"截图保存失败: {exc}")

    def open_venue(self, *, ready_timeout_ms: int = 8000) -> None:
        assert self.page is not None
        self.log(f"打开羽毛球场直达页: {self.config.venue_url}")
        self.page.goto(self.config.venue_url, wait_until="domcontentloaded")
        if ready_timeout_ms > 0:
            self.wait_for_direct_page_ready(timeout_ms=ready_timeout_ms)
            self.dismiss_reading_notice(timeout_ms=0)

    def wait_for_direct_page_ready(self, *, timeout_ms: int = 8000) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        password_selectors = [
            'input[type="password"]',
            'input[placeholder*="密码"]',
        ]
        while time.monotonic() <= deadline:
            if self.is_booking_page():
                return
            if self.first_visible_locator(password_selectors):
                return
            text = self.current_text(timeout_ms=100)
            if "统一身份认证" in text or "用户名" in text or "请输入密码" in text:
                return
            time.sleep(0.1)

    def login_if_needed(self) -> None:
        assert self.page is not None
        if self.is_logged_in():
            self.log("已处于登录状态。")
            self.dismiss_reading_notice(timeout_ms=1000)
            return

        if not self.config.username or not self.config.password:
            raise BotError("需要在 .env 中填写 FUDAN_USERNAME 和 FUDAN_PASSWORD。")

        username_selectors = [
            'input[name="username"]',
            'input#username',
            'input[name="userName"]',
            'input[name="loginName"]',
            'input[placeholder*="账号"]',
            'input[placeholder*="学号"]',
            'input[placeholder*="工号"]',
            'input[placeholder*="用户名"]',
            'input[type="text"]',
        ]
        password_selectors = [
            'input[name="password"]',
            'input#password',
            'input[placeholder*="密码"]',
            'input[type="password"]',
        ]
        if not self.first_visible_locator(password_selectors):
            self.log("尝试打开账号密码登录页。")
            self.click_first_text(["Sign in", "登录", "统一身份认证", "账号登录", "密码登录", "用户名密码登录"], required=False)
            self.wait_network_idle(timeout_ms=10000)

        self.log("尝试使用账号密码登录。")
        username_input = self.first_visible_locator_with_timeout(username_selectors, timeout_ms=10000)
        password_input = self.first_visible_locator_with_timeout(password_selectors, timeout_ms=10000)
        if not username_input or not password_input:
            raise BotError("没有找到账号密码输入框。若页面为空白，通常是站点风控拦截了 headless 浏览器。")

        username_input.fill(self.config.username)
        password_input.fill(self.config.password)

        submit = self.first_visible_locator([
            'button:has-text("登录")',
            'input[type="submit"]',
            'button[type="submit"]',
            'a:has-text("登录")',
        ])
        if not submit:
            raise BotError("没有找到登录按钮。")

        submit.click()
        self.wait_network_idle(timeout_ms=15000)
        if not self.is_logged_in():
            raise BotError("登录后仍未检测到已登录状态。可能需要验证码、短信验证或页面结构已变化。")
        self.log("登录成功。")
        self.dismiss_reading_notice(timeout_ms=1000)

    def is_logged_in(self) -> bool:
        assert self.page is not None
        if self.is_booking_page():
            return True
        if self.first_visible_locator(['input[type="password"]']):
            return False
        login_locator = self.page.get_by_text("登录", exact=True).first
        if self.locator_is_visible(login_locator, timeout_ms=300):
            return False
        logged_in_markers = [
            "退出",
            "用户中心",
            "我的预约",
            "服务中心",
            "我的收藏",
            "待办任务",
            "个人数据中心",
            "全部服务",
        ]
        for text in logged_in_markers:
            if self.has_visible_text(text, timeout_ms=800):
                return True
        return False

    def is_booking_page(self) -> bool:
        text = self.current_text(timeout_ms=300)
        return self.config.venue_keyword in text and "个人预约" in text and "时段" in text

    def wait_until_open_time(self) -> None:
        open_dt = self.today_open_datetime()
        now = datetime.now(self.tz)
        if now >= open_dt:
            return
        seconds = (open_dt - now).total_seconds()
        self.log(f"等待开放时间 {open_dt.strftime('%H:%M:%S')}，剩余 {seconds:.1f} 秒。")
        time.sleep(seconds)

    def today_open_datetime(self) -> datetime:
        parts = [int(part) for part in self.config.open_time.split(":")]
        if len(parts) == 2:
            parts.append(0)
        now = datetime.now(self.tz)
        return now.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)

    def booking_deadline(self) -> float:
        remaining = self.config.retry_until_seconds
        if self.config.wait_until_open:
            remaining = (
                self.today_open_datetime()
                + timedelta(seconds=remaining) - datetime.now(self.tz)
            ).total_seconds()
        return time.monotonic() + max(0, remaining)

    @staticmethod
    def is_navigation_race(exc: PlaywrightError) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in (
            "frame was detached", "frame has been detached", "frame got detached",
            "execution context was destroyed", "cannot find context with specified id",
            "interrupted by another navigation", "element is not attached to the dom",
        ))

    def retry_delay(self, idle_rounds: int, *, all_full: bool) -> float:
        base = max(1.0, self.config.retry_interval_seconds, 3.0 if all_full else 0)
        ceiling = max(base, self.config.retry_max_interval_seconds)
        delay = min(ceiling, base * 2 ** min(max(0, idle_rounds - 1), 10))
        return delay + random.uniform(0, max(0, self.config.retry_jitter_seconds))

    def book_with_retries(self) -> list[str]:
        assert self.page is not None
        booked: list[str] = []
        conflicts: set[str] = set()
        self.pending_slots.clear()
        self.all_slots_full = False
        deadline = self.booking_deadline()
        attempt = 0
        reopen = False
        stop = False
        idle_rounds = 0
        full_rounds = 0
        busy_rounds = 0
        self.log(f"本轮最多预约 {self.config.max_slots} 个时段，"
                 f"按顺序尝试: {', '.join(self.config.preferred_slots)}；"
                 f"剩余提交窗口 {max(0, deadline - time.monotonic()):.1f} 秒。")

        def has_capacity() -> bool:
            return len(booked) + len(self.pending_slots) < self.config.max_slots

        while has_capacity() and (self.dry_run or time.monotonic() < deadline):
            attempt += 1
            self.log(f"第 {attempt} 轮按配置顺序检查 {self.target_date} 的可预约时段。")
            scope = None
            snapshot: dict[str, dict[str, str]] = {}
            all_full = True
            checked = 0
            submitted = False
            busy = False
            # Preserve the configured order on EVERY pass. Each slot is submitted
            # at most once per pass; definitive failures advance to the next slot.
            for slot in self.config.preferred_slots:
                if not has_capacity() or (not self.dry_run and time.monotonic() >= deadline):
                    break
                if slot in booked or slot in conflicts or slot in self.pending_slots:
                    continue
                if scope is None:
                    timeout_ms = self.config.booking_ready_timeout_ms
                    if not self.dry_run:
                        timeout_ms = min(timeout_ms, max(1, int((deadline - time.monotonic()) * 1000)))
                    try:
                        if reopen:
                            self.open_venue(ready_timeout_ms=0)
                            reopen = False
                        scope = self.ensure_target_date_visible(timeout_ms=timeout_ms)
                        snapshot = self.read_slots(scope)
                    except CalendarNotReady:
                        all_full = False
                        self.log(f"等待目标日期超时，当前表格日期: {', '.join(self.visible_booking_dates()) or '无'}")
                        break
                    except PlaywrightError as exc:
                        if not self.is_navigation_race(exc):
                            raise
                        all_full = False
                        self.log("读取场次时页面正在切换，丢弃旧页面引用后重试。")
                        break
                candidate = snapshot.get(slot, {"status": "not-found", "text": ""})
                checked += 1
                all_full = all_full and candidate["status"] == "unavailable" and bool(
                    re.search(r"约满|已满", candidate["text"])
                )
                if candidate["status"] != "available":
                    self.log(f"{slot} 跳过: {candidate['status']} {candidate['text']}")
                    if candidate["status"] == "not-ready":
                        break
                    continue
                if self.dry_run:
                    self.log(f"[dry-run] 会按顺序尝试预约 {slot}，单元格文本: {candidate['text']}")
                    continue
                if time.monotonic() >= deadline:
                    break
                try:
                    selected = self.click_slot(scope, slot)
                except PlaywrightError as exc:
                    if not isinstance(exc, PlaywrightTimeoutError) and not self.is_navigation_race(exc):
                        raise
                    self.log("选择时段时页面切换或点击超时，重新读取后再尝试；尚未提交预约。")
                    reopen = True
                    break
                if selected["status"] != "clicked":
                    self.log(f"{slot} 点击前状态已变化: {selected['status']} {selected['text']}")
                    continue
                started = time.monotonic()
                submitted = True
                self.log(f"开始提交 {slot}，点击前余量: {selected.get('text', '')}")
                result = self.submit_booking(slot)
                self.log(f"{slot} 提交结果: {result.status}，耗时 {time.monotonic() - started:.2f} 秒，"
                         f"{result.feedback or '无明确反馈'}")
                if result.status == "confirmed":
                    booked.append(slot)
                elif result.status == "limit":
                    self.log("预约系统提示账号额度已达上限，停止本轮提交。")
                    stop = True
                    break
                elif result.status == "conflict":
                    conflicts.add(slot)
                elif result.status == "unknown":
                    self.pending_slots.add(slot)
                    self.log(f"{slot} 结果待核实，暂占一个预约名额，本轮不重复提交。")
                elif result.status == "busy":
                    # The site explicitly declined this request due to load.
                    # Cool down before any more submissions, then preserve the
                    # original priorities using a fresh availability snapshot.
                    busy = True
                    reopen = True
                    self.log("网站提示拥挤，本次未获受理，不占预约名额；退避后按原优先级重试。")
                    break
                # Navigation clears the old selection and any previous feedback.
                # Do it only when there is another candidate to inspect.
                scope = None
                reopen = True

            if self.dry_run or stop or not has_capacity() or time.monotonic() >= deadline:
                break
            if all(slot in booked or slot in conflicts or slot in self.pending_slots
                   for slot in self.config.preferred_slots):
                break
            all_full = all_full and checked > 0
            full_rounds = full_rounds + 1 if all_full else 0
            if self.config.full_slot_checks > 0 and full_rounds >= self.config.full_slot_checks:
                self.all_slots_full = True
                self.log(f"连续 {full_rounds} 轮确认剩余目标时段全部约满，正常结束检查。")
                break
            idle_rounds = 0 if submitted else idle_rounds + 1
            busy_rounds = busy_rounds + 1 if busy else 0
            delay = min(self.retry_delay(busy_rounds if busy else idle_rounds, all_full=all_full),
                        max(0, deadline - time.monotonic()))
            self.log(f"等待 {delay:.1f} 秒后再次检查。")
            time.sleep(delay)
            if time.monotonic() >= deadline:
                break
            if not reopen:
                try:
                    self.refresh_booking_page()
                except PlaywrightError as exc:
                    if not self.is_navigation_race(exc):
                        raise
                    self.log("刷新被页面跳转打断，下一轮等待当前页面加载。")

        booked = self.reconcile_pending_slots(booked)
        return [slot for slot in self.config.preferred_slots if slot in booked]

    def reconcile_pending_slots(self, booked: list[str]) -> list[str]:
        if not self.pending_slots or self.dry_run:
            return booked
        assert self.page is not None
        self.log("打开我的预约，核实本轮结果不明的提交。")
        locator = self.first_visible_text_locator("我的预约")
        if not locator:
            return booked
        try:
            locator.click()
            deadline = time.monotonic() + self.config.submit_result_timeout_ms / 1000
            while self.pending_slots and time.monotonic() <= deadline:
                for slot in list(self.pending_slots):
                    if self.confirmed_appointment_record_text(slot):
                        self.pending_slots.remove(slot)
                        booked.append(slot)
                        self.log(f"{slot} 已通过预约记录补充确认成功。")
                if self.pending_slots:
                    time.sleep(0.1)
        except Exception as exc:
            self.log(f"核实预约记录未完成: {type(exc).__name__}")
        return booked

    def ensure_target_date_visible(self, *, timeout_ms: int | None = None) -> Any:
        assert self.page is not None
        timeout_ms = timeout_ms or self.config.booking_ready_timeout_ms
        deadline = time.monotonic() + timeout_ms / 1000
        week_turns = 0
        previous_week: list[str] | None = None
        started = time.monotonic()

        while time.monotonic() <= deadline:
            self.dismiss_reading_notice(timeout_ms=0)
            scope = self.scope_with_text(self.target_date)
            states = self.read_slots(scope) if scope else {}
            if (any(item["status"] in {"available", "unavailable"} for item in states.values())
                    and not any(item["status"] == "not-ready" for item in states.values())):
                elapsed = time.monotonic() - started
                if elapsed >= 1:
                    self.log(f"目标日期和时段已加载，等待耗时 {elapsed:.1f} 秒。")
                return scope

            visible_dates = self.visible_booking_dates()
            # Do not click the week button again while the previous turn is loading.
            if visible_dates == previous_week:
                time.sleep(0.1)
                continue
            if week_turns < 5 and visible_dates:
                if self.target_date < visible_dates[0]:
                    if self.click_first_text(["前一周", "上一周", "上周"], required=False, expect_new_page=False):
                        week_turns += 1
                        previous_week = visible_dates
                        continue
                elif self.target_date > visible_dates[-1]:
                    if self.click_first_text(["后一周", "下一周", "下周"], required=False, expect_new_page=False):
                        week_turns += 1
                        previous_week = visible_dates
                        continue
            time.sleep(0.1)
        raise CalendarNotReady(f"没有在预约表格中找到目标日期: {self.target_date}")

    def read_slots(self, scope: Any) -> dict[str, dict[str, str]]:
        try:
            return scope.evaluate(CALENDAR_SCRIPT, {
                "dateText": self.target_date,
                "slotTexts": self.config.preferred_slots,
                "action": "read",
            })
        except PlaywrightError as exc:
            if not self.is_navigation_race(exc):
                raise
            return {slot: {"status": "not-ready", "text": "页面正在切换，等待重新定位"}
                    for slot in self.config.preferred_slots}

    def click_slot(self, scope: Any, slot: str) -> dict[str, str]:
        # Recheck availability at the moment of selection, even after a snapshot.
        args = {
            "dateText": self.target_date, "slotTexts": [slot],
            "action": "read",
        }
        state = scope.evaluate(CALENDAR_SCRIPT, args)[slot]
        if state["status"] != "available":
            return state
        if self.dry_run:
            return {**state, "status": "would-click"}
        handle = scope.evaluate_handle(CALENDAR_SCRIPT, {**args, "action": "locate"})
        try:
            element = handle.as_element()
            if element is None:
                return {"status": "not-ready", "text": "点击前场次状态已变化"}
            # Playwright scrolls into view and waits for visibility, stability and
            # hit testing before sending an ordinary browser mouse click.
            element.click(timeout=self.config.action_timeout_ms, delay=self.config.click_delay_ms)
            return {**state, "status": "clicked"}
        finally:
            handle.dispose()

    def submit_booking(self, slot: str) -> SubmitResult:
        if self.dry_run:
            return SubmitResult("not-submitted", "dry-run")
        assert self.page is not None
        try:
            baseline_feedback = self.feedback_text()
            self.fill_phone_if_needed()
        except PlaywrightError as exc:
            if not self.is_navigation_race(exc):
                raise
            return SubmitResult("not-submitted", "提交前页面已切换，尚未点击提交")
        try:
            clicked = self.click_submit_button()
            if not clicked:
                return SubmitResult("not-submitted", "没有找到可点击的提交按钮")
            return self.wait_for_submit_result(slot, baseline_feedback=baseline_feedback, confirm_dialog=True)
        except PlaywrightError as exc:
            # Once a click may have been sent, retrying it is unsafe. Verify the
            # appointment later, even when the click itself reported a timeout.
            return SubmitResult("unknown", f"提交期间页面异常，需核对预约记录: {str(exc).splitlines()[0]}")

    @staticmethod
    def classify_submit_feedback(feedback: str) -> str:
        if "达到上限" in feedback or "达上限" in feedback:
            return "limit"
        if any(word in feedback for word in ("不可重叠", "冲突", "重复预约")):
            return "conflict"
        if any(word in feedback for word in (
            "失败", "错误", "约满", "已满", "不可预约", "未开放", "重复", "超过",
            "剩余资源容量不足", "最多预约1个时段",
        )):
            return "rejected"
        # Keep this narrow: a network timeout or a processing message does NOT
        # establish rejection and must still reserve capacity as "unknown".
        normalized = re.sub(r"[\s，,。.!！]", "", feedback)
        if "前方拥挤请稍后再试" in normalized:
            return "busy"
        return "unknown"

    def wait_for_submit_result(
        self,
        slot: str,
        *,
        baseline_feedback: str = "",
        timeout_ms: int | None = None,
        confirm_dialog: bool = False,
    ) -> SubmitResult:
        timeout_ms = timeout_ms or self.config.submit_result_timeout_ms
        deadline = time.monotonic() + timeout_ms / 1000
        last_feedback = ""
        dialog_confirmed = False

        while time.monotonic() <= deadline:
            record_text = self.confirmed_appointment_record_text(slot)
            if record_text:
                return SubmitResult("confirmed", "检测到目标日期、场馆和时段的待签到预约记录")
            feedback = self.feedback_text()
            new_feedback = self.feedback_without_baseline(feedback, baseline_feedback)
            if new_feedback:
                last_feedback = new_feedback
                status = self.classify_submit_feedback(new_feedback)
                if status != "unknown":
                    return SubmitResult(status, new_feedback)

            if confirm_dialog and not dialog_confirmed:
                dialog_confirmed = self.confirm_submit_if_needed(timeout_ms=0)
            time.sleep(0.1)

        return SubmitResult("unknown", last_feedback or "等待预约记录超时")

    def feedback_without_baseline(self, feedback: str, baseline_feedback: str) -> str:
        if not feedback:
            return ""
        if not baseline_feedback:
            return feedback
        chunks = [chunk.strip() for chunk in feedback.split(" | ") if chunk.strip()]
        baseline_chunks = {chunk.strip() for chunk in baseline_feedback.split(" | ")}
        new_chunks = [chunk for chunk in chunks if chunk not in baseline_chunks]
        return " | ".join(new_chunks)

    def confirmed_appointment_record_text(self, slot: str) -> str:
        assert self.page is not None
        script = """
        ({ dateText, slotText, venueText }) => {
          const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const selectors = [
            'tr',
            '.el-table__row',
            '.ant-table-row',
            '.van-cell',
            '.list-item',
            'li',
            '[class*="reservation"]',
            '[class*="order"]',
            '[class*="record"]',
            '[class*="item"]'
          ];
          const candidates = Array.from(document.querySelectorAll(selectors.join(',')))
            .filter((element) => visible(element))
            .filter((element) => !element.querySelector('tr, .el-table__row, .ant-table-row'))
            .map((element) => normalize(element.innerText || element.textContent))
            .filter((text) => text.includes(dateText + ' ' + slotText)
              && text.includes(venueText)
              && text.includes('待签到')
              && text.includes('已预约'))
            .sort((a, b) => a.length - b.length);
          for (const text of candidates) {
            if (text.length <= 1200) return text;
          }
          return '';
        }
        """
        for scope in self.scopes():
            try:
                text = scope.evaluate(script, {
                    "dateText": self.target_date, "slotText": slot,
                    "venueText": self.config.venue_keyword,
                })
            except Exception:
                continue
            if text:
                return text
        return ""

    def feedback_text(self) -> str:
        script = """
        () => {
          const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const selectors = [
            '.el-message',
            '.el-notification',
            '.el-dialog',
            '.ant-message',
            '.ant-notification',
            '.ant-modal',
            '[role="alert"]',
            '[role="dialog"]',
            '.toast',
            '.modal'
          ];
          const texts = [];
          for (const selector of selectors) {
            for (const element of document.querySelectorAll(selector)) {
              if (visible(element)) {
                const text = normalize(element.innerText || element.textContent);
                if (text) texts.push(text);
              }
            }
          }
          return Array.from(new Set(texts)).join(' | ');
        }
        """
        chunks = []
        for scope in self.scopes():
            try:
                text = scope.evaluate(script)
            except Exception:
                continue
            if text:
                chunks.append(text)
        return " | ".join(chunks)

    def enabled_button(self, scope: Any, name: re.Pattern[str]) -> Any | None:
        for role in ("button", "link"):
            try:
                candidates = scope.get_by_role(role, name=name).all()
                for button in candidates:
                    if button.is_visible() and button.is_enabled():
                        return button
            except PlaywrightError as exc:
                if not self.is_navigation_race(exc):
                    raise
        return None

    def confirm_submit_if_needed(self, *, timeout_ms: int = 1000) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        selectors = '.el-dialog, .el-message-box, .ant-modal, .van-dialog, [role="dialog"], .modal'
        while True:
            for scope in self.scopes():
                try:
                    dialogs = scope.locator(selectors).all()
                    button = None
                    for dialog in dialogs:
                        if not dialog.is_visible():
                            continue
                        text = dialog.inner_text(timeout=300)
                        if not re.search(r"预约|提交", text) or re.search(r"阅读须知|取消预约", text):
                            continue
                        button = self.enabled_button(dialog, re.compile(r"^(确定|确认|我知道了)$"))
                        if button is not None:
                            break
                except PlaywrightError as exc:
                    if not self.is_navigation_race(exc):
                        raise
                    continue
                if button is not None:
                    # Never retry an ambiguous click inside this method.
                    button.click(timeout=self.config.action_timeout_ms, delay=self.config.click_delay_ms)
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)

    def fill_phone_if_needed(self) -> None:
        if not self.config.phone:
            return
        for scope in self.scopes():
            phone_input = self.first_visible_locator([
                'input[placeholder*="手机"]',
                'input[placeholder*="电话"]',
                'input[name*="phone" i]',
                'input[id*="phone" i]',
                'input[type="tel"]',
            ], scopes=[scope])
            if not phone_input:
                continue
            try:
                current = phone_input.input_value(timeout=500)
            except Exception:
                current = ""
            if not current.strip():
                phone_input.fill(self.config.phone)
            return

    def click_submit_button(self) -> bool:
        deadline = time.monotonic() + 1.0
        while time.monotonic() <= deadline:
            for scope in self.scopes():
                button = self.enabled_button(scope, re.compile(r"提交预约|确认预约|立即预约|^提交$"))
                if button is not None:
                    button.click(timeout=self.config.action_timeout_ms, delay=self.config.click_delay_ms)
                    return True
            time.sleep(0.1)
        return False

    def refresh_booking_page(self) -> None:
        assert self.page is not None
        try:
            self.page.reload(wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            pass

    def dismiss_reading_notice(self, *, timeout_ms: int = 500) -> bool:
        assert self.page is not None
        script = """
        (labels) => {
          if (!document.body.innerText.includes('阅读须知')) return null;
          const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const isEnabled = (element) => !element.disabled && element.getAttribute('aria-disabled') !== 'true';
          const buttonText = (element) => normalize(
            element.innerText || element.value || element.getAttribute('aria-label') || element.textContent
          );
          const findConfirmButton = (container) => {
            const buttons = Array.from(container.querySelectorAll(
              'button, input[type="button"], input[type="submit"], a, [role="button"]'
            )).filter((element) => visible(element) && isEnabled(element));
            for (const label of labels) {
              const target = buttons.find((element) => buttonText(element) === label);
              if (target) return target;
            }
            return null;
          };

          const dialogSelectors = [
            '.el-dialog',
            '.el-message-box',
            '.ant-modal',
            '.van-dialog',
            '.arco-modal',
            '.ivu-modal',
            '.layui-layer',
            '.n-modal',
            '.semi-modal',
            '[role="dialog"]',
            '.modal'
          ];
          const dialogs = Array.from(document.querySelectorAll(dialogSelectors.join(',')))
            .filter((element) => visible(element))
            .filter((element) => normalize(element.innerText || element.textContent).includes('阅读须知'));
          for (const dialog of dialogs) {
            const target = findConfirmButton(dialog);
            if (target) return target;
          }

          const noticeElements = Array.from(document.querySelectorAll('body *'))
            .filter((element) => visible(element))
            .map((element) => ({ element, text: normalize(element.innerText || element.textContent) }))
            .filter((item) => item.text.includes('阅读须知'))
            .sort((a, b) => a.text.length - b.text.length);
          for (const item of noticeElements) {
            let container = item.element;
            for (let depth = 0; depth < 10 && container && container !== document.body; depth += 1) {
              const text = normalize(container.innerText || container.textContent);
              if (text.includes('阅读须知')) {
                const target = findConfirmButton(container);
                if (target) return target;
              }
              container = container.parentElement;
            }
          }
          return null;
        }
        """
        deadline = time.monotonic() + timeout_ms / 1000
        labels = ["确定", "同意", "我知道了", "已阅读"]
        while True:
            for scope in self.scopes():
                try:
                    handle = scope.evaluate_handle(script, labels)
                    try:
                        element = handle.as_element()
                        if element is not None:
                            element.click(timeout=self.config.action_timeout_ms, delay=self.config.click_delay_ms)
                            self.log("已确认阅读须知。")
                            return True
                    finally:
                        handle.dispose()
                except PlaywrightError as exc:
                    if not self.is_navigation_race(exc):
                        raise
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        return False

    def wait_network_idle(self, *, timeout_ms: int) -> None:
        assert self.page is not None
        timeout_ms = min(timeout_ms, self.config.page_settle_timeout_ms)
        if timeout_ms <= 0:
            return
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass

    def click_first_text(self, texts: list[str], *, required: bool, expect_new_page: bool = True) -> bool:
        assert self.page is not None
        for text in texts:
            locator = self.first_visible_text_locator(text)
            if not locator:
                continue
            if not expect_new_page:
                try:
                    locator.click(delay=self.config.click_delay_ms)
                    return True
                except PlaywrightError as exc:
                    if not self.is_navigation_race(exc):
                        raise
                    return False
            try:
                with self.page.context.expect_page(timeout=1500) as new_page_info:
                    locator.click()
                new_page = new_page_info.value
                new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                self.page = new_page
            except PlaywrightTimeoutError:
                pass
            return True
        if required:
            raise BotError(f"没有找到可点击文本: {', '.join(texts)}")
        return False

    def first_visible_text_locator(self, text: str) -> Any | None:
        for scope in self.scopes():
            locator = scope.get_by_text(text, exact=False).first
            if self.locator_is_visible(locator):
                return locator
        return None

    def first_visible_locator(self, selectors: list[str], scopes: list[Any] | None = None) -> Any | None:
        for scope in scopes or self.scopes():
            for selector in selectors:
                locator = scope.locator(selector).first
                if self.locator_is_visible(locator):
                    return locator
        return None

    def first_visible_locator_with_timeout(self, selectors: list[str], *, timeout_ms: int) -> Any | None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() <= deadline:
            locator = self.first_visible_locator(selectors)
            if locator:
                return locator
            time.sleep(0.1)
        return None

    def locator_is_visible(self, locator: Any, timeout_ms: int = 500) -> bool:
        try:
            return locator.is_visible(timeout=timeout_ms)
        except Exception:
            return False

    def has_visible_text(self, text: str, *, timeout_ms: int) -> bool:
        return self.first_visible_text_locator_with_timeout(text, timeout_ms) is not None

    def first_visible_text_locator_with_timeout(self, text: str, timeout_ms: int) -> Any | None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() <= deadline:
            locator = self.first_visible_text_locator(text)
            if locator:
                return locator
            time.sleep(0.1)
        return None

    def scope_with_text(self, text: str) -> Any | None:
        for scope in self.scopes():
            # A hidden duplicate must not hide a visible date further down the DOM.
            try:
                for locator in scope.get_by_text(text, exact=False).all():
                    if self.locator_is_visible(locator, timeout_ms=300):
                        return scope
            except PlaywrightError as exc:
                if not self.is_navigation_race(exc):
                    raise
        return None

    def visible_booking_dates(self) -> list[str]:
        dates: set[str] = set()
        for scope in self.scopes():
            try:
                texts = scope.locator(".week_header:visible, thead:visible").all_inner_texts()
            except Exception:
                continue
            for text in texts:
                dates.update(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", text))
        return sorted(dates)

    def scopes(self) -> list[Any]:
        assert self.page is not None
        # The list is only a snapshot: callers must still handle a detach during
        # the following DOM operation. Prioritize the main page over iframes.
        return [frame for frame in self.page.frames if not frame.is_detached()]

    def current_text(self, *, timeout_ms: int = 1000) -> str:
        chunks = []
        for scope in self.scopes():
            try:
                chunks.append(scope.locator("body").inner_text(timeout=timeout_ms))
            except Exception:
                continue
        return "\n".join(chunks)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="自动预约复旦江湾羽毛球场地")
    parser.add_argument("--env-file", default=".env", help="环境变量文件路径")
    parser.add_argument("--target-date", help="指定预约日期，格式 YYYY-MM-DD；默认今天 + FUDAN_TARGET_DAYS_AHEAD")
    parser.add_argument("--dry-run", action="store_true", help="只检查并打印会点击的时段，不提交预约")
    parser.add_argument("--login-only", action="store_true", help="只登录并进入预约页，不提交预约")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口，覆盖 FUDAN_HEADLESS")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config = load_config(Path(args.env_file))
    if args.headed:
        config = Config(**{**config.__dict__, "headless": False, "use_cdp_browser": True})

    bot = FudanBadmintonBot(config, dry_run=args.dry_run, target_date=args.target_date)
    return bot.run_once(login_only=args.login_only)


if __name__ == "__main__":
    raise SystemExit(main())
