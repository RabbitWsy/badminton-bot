from __future__ import annotations

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
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
    pre_open_refresh_ms: int
    retry_until_seconds: float
    retry_interval_seconds: float
    page_settle_timeout_ms: int
    submit_result_timeout_ms: int
    force_login: bool
    headless: bool
    slow_mo_ms: int
    browser_channel: str
    browser_executable_path: str
    storage_state: Path
    log_dir: Path
    navigation_timeout_ms: int = 30000
    action_timeout_ms: int = 5000


class BotError(RuntimeError):
    pass


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_slots(raw: str | None) -> list[str]:
    if not raw:
        return DEFAULT_PREFERRED_SLOTS.copy()
    return [item.strip() for item in raw.split(",") if item.strip()]


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
        pre_open_refresh_ms=int(os.getenv("FUDAN_PRE_OPEN_REFRESH_MS", "1800")),
        retry_until_seconds=float(os.getenv("FUDAN_RETRY_UNTIL_SECONDS", "45")),
        retry_interval_seconds=float(os.getenv("FUDAN_RETRY_INTERVAL_SECONDS", "0.5")),
        page_settle_timeout_ms=int(os.getenv("FUDAN_PAGE_SETTLE_TIMEOUT_MS", "1200")),
        submit_result_timeout_ms=int(os.getenv("FUDAN_SUBMIT_RESULT_TIMEOUT_MS", "1500")),
        force_login=parse_bool(os.getenv("FUDAN_FORCE_LOGIN"), True),
        headless=parse_bool(os.getenv("FUDAN_HEADLESS"), True),
        slow_mo_ms=int(os.getenv("FUDAN_SLOW_MO_MS", "0")),
        browser_channel=os.getenv("FUDAN_BROWSER_CHANNEL", "").strip(),
        browser_executable_path=os.getenv("FUDAN_BROWSER_EXECUTABLE_PATH", "").strip(),
        storage_state=root / os.getenv("FUDAN_STORAGE_STATE", "storage_state.json"),
        log_dir=root / os.getenv("FUDAN_LOG_DIR", "logs"),
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

    def run_once(self, *, login_only: bool = False) -> int:
        with sync_playwright() as playwright:
            launch_kwargs: dict[str, Any] = {
                "headless": self.config.headless,
                "slow_mo": self.config.slow_mo_ms,
            }
            if self.config.browser_channel:
                launch_kwargs["channel"] = self.config.browser_channel
            if self.config.browser_executable_path:
                launch_kwargs["executable_path"] = self.config.browser_executable_path
            browser = playwright.chromium.launch(**launch_kwargs)
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": 1440, "height": 1100},
                "locale": "zh-CN",
                "timezone_id": self.config.timezone,
            }
            if self.config.storage_state.exists() and not self.config.force_login:
                context_kwargs["storage_state"] = str(self.config.storage_state)

            context = browser.new_context(**context_kwargs)
            context.set_default_timeout(self.config.action_timeout_ms)
            context.set_default_navigation_timeout(self.config.navigation_timeout_ms)
            self.page = context.new_page()

            try:
                if self.config.force_login:
                    self.log("本次运行强制重新登录，不使用旧 storage_state 启动。")
                self.open_venue()
                self.login_if_needed()
                context.storage_state(path=str(self.config.storage_state))
                if login_only:
                    self.log("登录态已保存。")
                    return 0

                if not self.is_booking_page():
                    self.open_venue()
                if self.config.wait_until_open:
                    self.wait_until_pre_open_refresh()
                    self.refresh_booking_page()
                    self.wait_until_open_time()

                booked = self.book_with_retries()
                if booked:
                    self.log(f"完成预约: {', '.join(booked)}")
                    return 0

                if self.dry_run:
                    self.save_screenshot("dry-run")
                    self.log("dry-run 完成，没有提交预约。")
                    return 0

                self.save_screenshot("no-slot-booked")
                self.log("没有成功预约任何时段。")
                return 2
            except Exception as exc:
                self.save_screenshot("error")
                self.log(f"失败: {exc}")
                raise
            finally:
                context.close()
                browser.close()

    def log(self, message: str) -> None:
        stamp = datetime.now(self.tz).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] {message}", flush=True)

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

    def open_venue(self) -> None:
        assert self.page is not None
        self.log(f"打开羽毛球场直达页: {self.config.venue_url}")
        self.page.goto(self.config.venue_url, wait_until="domcontentloaded")
        self.wait_network_idle(timeout_ms=10000)
        self.wait_for_direct_page_ready()

    def wait_for_direct_page_ready(self, *, timeout_ms: int = 8000) -> None:
        deadline = time.time() + timeout_ms / 1000
        password_selectors = [
            'input[type="password"]',
            'input[placeholder*="密码"]',
        ]
        while time.time() <= deadline:
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
            self.click_first_text(["登录", "统一身份认证", "账号登录", "密码登录", "用户名密码登录"], required=False)
            self.wait_network_idle(timeout_ms=10000)

        self.log("尝试使用账号密码登录。")
        username_input = self.first_visible_locator_with_timeout(username_selectors, timeout_ms=10000)
        password_input = self.first_visible_locator_with_timeout(password_selectors, timeout_ms=10000)
        if not username_input or not password_input:
            raise BotError("没有找到账号密码输入框。若页面要求验证码或扫码，请手动登录后保存 storage_state。")

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

    def wait_until_pre_open_refresh(self) -> None:
        open_dt = self.today_open_datetime()
        lead = max(0, self.config.pre_open_refresh_ms) / 1000
        refresh_dt = open_dt - timedelta(seconds=lead)
        now = datetime.now(self.tz)
        if now < refresh_dt:
            seconds = (refresh_dt - now).total_seconds()
            self.log(
                f"等待开放前刷新时间 {refresh_dt.strftime('%H:%M:%S.%f')[:-3]}，"
                f"剩余 {seconds:.1f} 秒。"
            )
            time.sleep(seconds)

    def today_open_datetime(self) -> datetime:
        parts = [int(part) for part in self.config.open_time.split(":")]
        if len(parts) == 2:
            parts.append(0)
        now = datetime.now(self.tz)
        return now.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)

    def book_with_retries(self) -> list[str]:
        assert self.page is not None
        booked: list[str] = []
        deadline = self.today_open_datetime() + timedelta(seconds=self.config.retry_until_seconds)
        attempt = 0

        while len(booked) < self.config.max_slots:
            now = datetime.now(self.tz)
            if attempt > 0 and now > deadline:
                break

            attempt += 1
            self.log(f"第 {attempt} 次检查 {self.target_date} 的可预约时段。")
            try:
                scope = self.ensure_target_date_visible()
            except BotError as exc:
                if "没有在预约表格中找到目标日期" not in str(exc):
                    raise
                self.log(f"暂未找到目标日期 {self.target_date}，刷新后继续重试。")
                time.sleep(max(0.1, self.config.retry_interval_seconds))
                self.refresh_booking_page()
                continue
            for slot in self.config.preferred_slots:
                if len(booked) >= self.config.max_slots:
                    break
                if slot in booked:
                    continue

                result = self.click_slot(scope, slot)
                status = result.get("status")
                text = result.get("text", "")
                if status == "click-point":
                    self.page.mouse.click(float(result["x"]), float(result["y"]))
                    status = "clicked"
                if status == "clicked":
                    if self.submit_booking(slot):
                        booked.append(slot)
                        self.save_screenshot(f"booked-{slot.replace(':', '')}")
                        if len(booked) < self.config.max_slots:
                            self.reopen_venue_after_success()
                            scope = self.ensure_target_date_visible()
                    else:
                        self.log(f"{slot} 已点击但未确认成功，页面反馈: {text}")
                elif status == "would-click":
                    self.log(f"[dry-run] 会尝试预约 {slot}，单元格文本: {text}")
                elif status == "unavailable":
                    self.log(f"{slot} 不可预约: {text}")
                elif status != "not-found":
                    self.log(f"{slot} 跳过: {status} {text}")

            if self.dry_run:
                break
            if len(booked) >= self.config.max_slots:
                break

            sleep_seconds = max(0.1, self.config.retry_interval_seconds)
            time.sleep(sleep_seconds)
            self.refresh_booking_page()

        return booked

    def reopen_venue_after_success(self) -> None:
        assert self.page is not None
        self.log("预约成功后重新打开羽毛球场直达页，准备继续下一段。")
        self.open_venue()

    def ensure_target_date_visible(self) -> Any:
        assert self.page is not None
        for _ in range(5):
            scope = self.scope_with_text(self.target_date)
            if scope:
                return scope
            if not self.click_first_text(["后一周", "下一周", "下周"], required=False):
                break
            self.wait_network_idle(timeout_ms=10000)
        raise BotError(f"没有在预约表格中找到目标日期: {self.target_date}")

    def click_slot(self, scope: Any, slot: str) -> dict[str, str]:
        script = """
        ({ dateText, slotText, dryRun }) => {
          const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const tables = Array.from(document.querySelectorAll('table'));
          for (const table of tables) {
            const rows = Array.from(table.querySelectorAll('tr'));
            let headerRowIndex = -1;
            let colIndex = -1;
            for (let rowIndex = 0; rowIndex < Math.min(rows.length, 6); rowIndex += 1) {
              const cells = Array.from(rows[rowIndex].children);
              for (let index = 0; index < cells.length; index += 1) {
                if (normalize(cells[index].innerText).includes(dateText)) {
                  headerRowIndex = rowIndex;
                  colIndex = index;
                  break;
                }
              }
              if (colIndex >= 0) break;
            }
            if (colIndex < 0) continue;

            for (let rowIndex = headerRowIndex + 1; rowIndex < rows.length; rowIndex += 1) {
              const cells = Array.from(rows[rowIndex].children);
              if (!cells.length) continue;
              const rowLabel = normalize(cells[0].innerText);
              if (!rowLabel.includes(slotText)) continue;

              const cell = cells[colIndex];
              if (!cell) return { status: 'missing-cell', text: '' };

              const text = normalize(cell.innerText);
              const disabledText = /(未开放|已过期|已满|不可预约|停用|关闭|无余量|暂无|冲突)/;
              const disabledElement = cell.matches('[disabled], .disabled, .is-disabled, [aria-disabled="true"]')
                || cell.querySelector('[disabled], .disabled, .is-disabled, [aria-disabled="true"]');
              if (disabledText.test(text) || disabledElement) {
                return { status: 'unavailable', text };
              }

              const clickable = Array.from(cell.querySelectorAll('button, a, [role="button"], input[type="button"], input[type="submit"]'))
                .find((element) => visible(element) && !element.disabled && element.getAttribute('aria-disabled') !== 'true')
                || cell;
              if (dryRun) return { status: 'would-click', text };
              clickable.scrollIntoView({ block: 'center', inline: 'center' });
              clickable.click();
              return { status: 'clicked', text };
            }
          }
          const textElements = Array.from(document.querySelectorAll('body *'))
            .filter((element) => visible(element))
            .map((element) => ({ element, text: normalize(element.innerText || element.textContent) }))
            .filter((item) => item.text);
          const dateItem = textElements
            .filter((item) => item.text.includes(dateText))
            .sort((a, b) => a.text.length - b.text.length)[0];
          const slotItem = textElements
            .filter((item) => item.text.includes(slotText))
            .sort((a, b) => a.text.length - b.text.length)[0];
          if (dateItem && slotItem) {
            slotItem.element.scrollIntoView({ block: 'center', inline: 'nearest' });
            const dateRect = dateItem.element.getBoundingClientRect();
            const slotRect = slotItem.element.getBoundingClientRect();
            const pointX = dateRect.left + dateRect.width / 2;
            const pointY = slotRect.top + slotRect.height / 2;
            let cell = document.elementFromPoint(pointX, pointY);
            if (!cell) return { status: 'missing-cell', text: '' };

            let clickable = cell;
            let text = normalize(cell.innerText || cell.textContent);
            for (let depth = 0; depth < 6 && clickable; depth += 1, clickable = clickable.parentElement) {
              const currentText = normalize(clickable.innerText || clickable.textContent);
              if (currentText && !currentText.includes(slotText) && !currentText.includes(dateText)) {
                text = currentText;
                break;
              }
            }

            const disabledText = /(未开放|已过期|已满|约满|不可预约|停用|关闭|无余量|暂无|冲突)/;
            if (disabledText.test(text)) {
              return { status: 'unavailable', text };
            }
            if (dryRun) return { status: 'would-click', text };
            return { status: 'click-point', text, x: pointX, y: pointY };
          }
          return { status: 'not-found', text: '' };
        }
        """
        return scope.evaluate(script, {
            "dateText": self.target_date,
            "slotText": slot,
            "dryRun": self.dry_run,
        })

    def submit_booking(self, slot: str) -> bool:
        if self.dry_run:
            return False
        assert self.page is not None
        self.fill_phone_if_needed()
        clicked = self.click_submit_button()
        if not clicked:
            self.log(f"{slot} 没有找到可点击的提交按钮。")
            return False

        self.click_first_text(["确定", "确认", "我知道了"], required=False)
        success, feedback = self.wait_for_submit_result(slot)
        if success:
            self.log(f"{slot} 预约成功。")
            return True
        if feedback:
            self.log(f"{slot} 提交后未成功，页面反馈: {feedback[:160]}")
        return False

    def wait_for_submit_result(self, slot: str) -> tuple[bool, str]:
        timeout_ms = self.config.submit_result_timeout_ms
        deadline = time.time() + timeout_ms / 1000
        last_feedback = ""
        success_words = ["预约成功", "提交成功", "成功预约", "预约已提交"]
        failure_words = ["失败", "错误", "约满", "已满", "不可预约", "未开放", "冲突", "重复", "超过"]

        while time.time() <= deadline:
            feedback = self.feedback_text()
            if feedback:
                last_feedback = feedback
                if any(word in feedback for word in success_words):
                    return True, feedback
                if any(word in feedback for word in failure_words):
                    return False, feedback

            body_text = self.current_text(timeout_ms=300)
            if any(word in body_text for word in success_words):
                return True, feedback or "检测到成功提示"
            if (
                "我的预约" in body_text
                and self.target_date in body_text
                and slot in body_text
                and any(word in body_text for word in ["已预约", "待签到"])
            ):
                return True, "检测到我的预约记录"
            time.sleep(0.1)

        return False, last_feedback

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
        script = """
        (labels) => {
          const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
          const visible = (element) => {
            const style = window.getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const elements = Array.from(document.querySelectorAll('button, input[type="button"], input[type="submit"], a, [role="button"]'))
            .filter((element) => visible(element) && !element.disabled && element.getAttribute('aria-disabled') !== 'true');
          for (const item of labels) {
            const target = elements.find((element) => {
              const text = normalize(element.innerText || element.value || element.getAttribute('aria-label'));
              return item.exact ? text === item.text : (text === item.text || text.includes(item.text));
            });
            if (target) {
              target.scrollIntoView({ block: 'center', inline: 'center' });
              target.click();
              return true;
            }
          }
          return false;
        }
        """
        labels = [
            {"text": "提交预约", "exact": False},
            {"text": "确认预约", "exact": False},
            {"text": "立即预约", "exact": False},
            {"text": "提交", "exact": True},
            {"text": "确定", "exact": True},
            {"text": "预约", "exact": True},
        ]
        deadline = time.time() + 0.6
        while time.time() <= deadline:
            for scope in self.scopes():
                try:
                    if scope.evaluate(script, labels):
                        return True
                except Exception:
                    continue
            time.sleep(0.05)
        return False

    def refresh_booking_page(self) -> None:
        assert self.page is not None
        try:
            self.page.reload(wait_until="domcontentloaded")
            self.wait_network_idle(timeout_ms=10000)
            self.wait_for_direct_page_ready()
        except PlaywrightTimeoutError:
            pass

    def wait_network_idle(self, *, timeout_ms: int) -> None:
        assert self.page is not None
        timeout_ms = min(timeout_ms, self.config.page_settle_timeout_ms)
        if timeout_ms <= 0:
            return
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass

    def click_first_text(self, texts: list[str], *, required: bool) -> bool:
        assert self.page is not None
        for text in texts:
            locator = self.first_visible_text_locator(text)
            if not locator:
                continue
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
        deadline = time.time() + timeout_ms / 1000
        while time.time() <= deadline:
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
        deadline = time.time() + timeout_ms / 1000
        while time.time() <= deadline:
            locator = self.first_visible_text_locator(text)
            if locator:
                return locator
            time.sleep(0.1)
        return None

    def scope_with_text(self, text: str) -> Any | None:
        for scope in self.scopes():
            locator = scope.get_by_text(text, exact=False).first
            if self.locator_is_visible(locator, timeout_ms=300):
                return scope
        return None

    def scopes(self) -> list[Any]:
        assert self.page is not None
        return list(self.page.frames)

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
    parser.add_argument("--login-only", action="store_true", help="只登录并保存 storage_state.json")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口，覆盖 FUDAN_HEADLESS")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config = load_config(Path(args.env_file))
    if args.headed:
        config = Config(**{**config.__dict__, "headless": False})

    bot = FudanBadmintonBot(config, dry_run=args.dry_run, target_date=args.target_date)
    return bot.run_once(login_only=args.login_only)


if __name__ == "__main__":
    raise SystemExit(main())
