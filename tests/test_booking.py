import tempfile
import time
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

from badminton_bot import FudanBadmintonBot, SubmitResult, load_config, parse_slots


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = replace(
            load_config(Path(self.temp.name) / "missing.env"),
            log_dir=Path(self.temp.name),
            preferred_slots=["21:00-22:30", "20:00-21:00", "19:00-20:00"],
            max_slots=2,
            submit_result_timeout_ms=4000,
            booking_ready_timeout_ms=6000,
            retry_until_seconds=90,
        )
        self.bot = FudanBadmintonBot(self.config, dry_run=False, target_date="2026-09-18")
        self.bot.log = Mock()


class RetryTests(BotTestCase):
    def setUp(self):
        super().setUp()
        self.bot.page = Mock()
        self.bot.today_open_datetime = lambda: datetime.now(self.bot.tz)
        for name in (
            "dismiss_reading_notice", "ensure_target_date_visible", "save_screenshot",
            "open_venue", "refresh_booking_page",
        ):
            setattr(self.bot, name, Mock())
        self.bot.reconcile_pending_slots = Mock(side_effect=lambda booked: booked)
        self.bot.click_slot = Mock(return_value={"status": "clicked", "text": "可预约 (8/15)"})
        self.bot.read_slots = Mock(return_value={
            slot: {"status": "available", "text": "可预约 (8/15)"}
            for slot in self.config.preferred_slots
        })
        self.bot.retry_delay = Mock(return_value=0)

    def submitted_slots(self):
        return [call.args[0] for call in self.bot.submit_booking.call_args_list]

    def test_failure_does_not_starve_other_slots(self):
        self.bot.submit_booking = Mock(side_effect=[
            SubmitResult("rejected", "剩余资源容量不足"),
            SubmitResult("confirmed"), SubmitResult("confirmed"),
        ])
        self.assertEqual(self.bot.book_with_retries(), ["20:00-21:00", "19:00-20:00"])
        self.assertEqual(self.submitted_slots(), self.config.preferred_slots)

    def test_overlap_is_skipped_and_not_counted_as_success(self):
        self.bot.submit_booking = Mock(side_effect=[
            SubmitResult("conflict", "预约时段不可重叠"),
            SubmitResult("confirmed"), SubmitResult("confirmed"),
        ])
        self.assertEqual(self.bot.book_with_retries(), ["20:00-21:00", "19:00-20:00"])
        self.assertEqual(self.submitted_slots(), self.config.preferred_slots)

    def test_account_limit_stops_all_further_submissions(self):
        self.bot.submit_booking = Mock(return_value=SubmitResult("limit", "达到上限（3）"))
        self.assertEqual(self.bot.book_with_retries(), [])
        self.assertEqual(self.submitted_slots(), ["21:00-22:30"])
        self.bot.open_venue.assert_not_called()

    def test_second_pass_restores_configured_order(self):
        # First pass: the top slot is available but rejected; the second is full.
        # Second pass: both are available. The top slot must still come first.
        available = self.bot.read_slots.return_value
        self.bot.read_slots.side_effect = [
            available,
            {slot: {"status": "unavailable", "text": "约满"} for slot in available},
            available, available,
        ]
        self.bot.submit_booking = Mock(side_effect=[
            SubmitResult("rejected"), SubmitResult("confirmed"), SubmitResult("confirmed"),
        ])
        self.assertEqual(self.bot.book_with_retries(), ["21:00-22:30", "20:00-21:00"])
        self.assertEqual(self.submitted_slots(), ["21:00-22:30", "21:00-22:30", "20:00-21:00"])

    def test_custom_order_is_never_sorted_by_time(self):
        self.bot.config = replace(self.config, preferred_slots=list(reversed(self.config.preferred_slots)))
        self.bot.submit_booking = Mock(return_value=SubmitResult("confirmed"))
        self.assertEqual(self.bot.book_with_retries(), ["19:00-20:00", "20:00-21:00"])
        self.assertEqual(self.submitted_slots(), ["19:00-20:00", "20:00-21:00"])
        self.bot.save_screenshot.assert_not_called()

    def test_deadline_prevents_new_submissions(self):
        self.bot.booking_deadline = lambda: time.monotonic() - 1
        self.bot.submit_booking = Mock()
        self.assertEqual(self.bot.book_with_retries(), [])
        self.bot.submit_booking.assert_not_called()

    def test_unopened_slot_does_not_hide_other_available_slots(self):
        self.bot.read_slots.return_value['21:00-22:30'] = {"status": "unavailable", "text": "未开放"}
        self.bot.submit_booking = Mock(return_value=SubmitResult('confirmed'))
        self.assertEqual(self.bot.book_with_retries(), ['20:00-21:00', '19:00-20:00'])
        self.assertEqual(self.submitted_slots(), ['20:00-21:00', '19:00-20:00'])

    def test_dry_run_only_inspects_one_pass(self):
        self.bot.dry_run = True
        self.bot.submit_booking = Mock()
        self.assertEqual(self.bot.book_with_retries(), [])
        self.bot.click_slot.assert_not_called()
        self.bot.submit_booking.assert_not_called()
        self.bot.refresh_booking_page.assert_not_called()

    def test_slot_config_deduplicates_without_reordering(self):
        self.assertEqual(parse_slots("20:00-21:00,21:00-22:30,20:00-21:00"),
                         ["20:00-21:00", "21:00-22:30"])

    def test_unknown_reserves_capacity_and_is_not_resubmitted(self):
        self.bot.submit_booking = Mock(side_effect=[SubmitResult("unknown"), SubmitResult("confirmed")])
        self.assertEqual(self.bot.book_with_retries(), ["20:00-21:00"])
        self.assertEqual(self.submitted_slots(), ["21:00-22:30", "20:00-21:00"])
        self.assertEqual(self.bot.pending_slots, {"21:00-22:30"})

    def test_feedback_classification(self):
        cases = {
            "预约时段不可重叠": "conflict",
            "有效期内爽约次数与已预约未开始时段数量合计达到上限（3）": "limit",
            "剩余资源容量不足，请重新预约": "rejected",
            "预约成功": "unknown",  # A toast alone is not a verified appointment.
            "已预约/点击查看详情": "unknown",
        }
        for feedback, status in cases.items():
            with self.subTest(feedback=feedback):
                self.assertEqual(self.bot.classify_submit_feedback(feedback), status)

    def test_all_full_stops_after_configured_checks_without_submitting(self):
        self.bot.read_slots.return_value = {
            slot: {"status": "unavailable", "text": "约满 (15/15)"}
            for slot in self.config.preferred_slots
        }
        self.bot.submit_booking = Mock()
        self.assertEqual(self.bot.book_with_retries(), [])
        self.assertEqual(self.bot.read_slots.call_count, 3)
        self.assertEqual(self.bot.refresh_booking_page.call_count, 2)
        self.bot.submit_booking.assert_not_called()
        self.assertTrue(self.bot.all_slots_full)

    def test_read_race_recovers_without_losing_slot_priority(self):
        self.bot.ensure_target_date_visible.side_effect = [
            PlaywrightError('Locator.all: Frame was detached'), Mock(), Mock(),
        ]
        self.bot.submit_booking = Mock(return_value=SubmitResult('confirmed'))
        self.assertEqual(self.bot.book_with_retries(), ['21:00-22:30', '20:00-21:00'])
        self.assertEqual(self.submitted_slots(), ['21:00-22:30', '20:00-21:00'])

    def test_click_race_does_not_submit_stale_selection(self):
        self.bot.click_slot.side_effect = [
            PlaywrightError('Element is not attached to the DOM'),
            {"status": "clicked", "text": "可预约"}, {"status": "clicked", "text": "可预约"},
        ]
        self.bot.submit_booking = Mock(return_value=SubmitResult('confirmed'))
        self.assertEqual(self.bot.book_with_retries(), ['21:00-22:30', '20:00-21:00'])
        self.assertEqual(self.submitted_slots(), ['21:00-22:30', '20:00-21:00'])

    def test_unrelated_playwright_error_is_not_silenced(self):
        self.bot.ensure_target_date_visible.side_effect = PlaywrightError('Invalid selector syntax')
        with self.assertRaisesRegex(PlaywrightError, 'Invalid selector'):
            self.bot.book_with_retries()

    def test_idle_backoff_is_bounded_and_full_checks_are_slower(self):
        self.bot.config = replace(self.config, retry_jitter_seconds=0)
        delay = lambda n, full=False: FudanBadmintonBot.retry_delay(self.bot, n, all_full=full)
        self.assertEqual([delay(n) for n in (1, 2, 3, 4, 100)], [1, 2, 4, 8, 8])
        self.assertEqual([delay(n, True) for n in (1, 2, 3)], [3, 6, 8])


class BrowserTests(BotTestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        super().setUp()
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.bot.page = self.page

    def record(self, *, date="2026-09-18", slot="21:00-22:30", venue=None, status="已预约 待签到"):
        venue = venue or self.config.venue_keyword
        return f"<table><tr><td>{venue}</td><td>{date} {slot}</td><td>{status}</td></tr></table>"

    def calendar(self, date="2026-09-18"):
        return (f'<div class="week_calendar"><dl class="left_time"><dt class="week_header">时段</dt>'
                '<dd>21:00-22:30</dd><dd>20:00-21:00</dd></dl>'
                f'<dl class="reservation_data"><dt class="week_header">{date}</dt>'
                '<dd class="can_active" onclick="window.clicked=\'21:00-22:30\'">可预约 (8/15)</dd>'
                '<dd class="no_active">约满 (15/15)</dd></dl></div>')

    def test_success_record_arriving_after_old_two_second_timeout(self):
        self.page.set_content('<div class="el-message">预约成功</div><div id="records"></div>')
        self.page.evaluate("html => setTimeout(() => records.innerHTML = html, 2500)", self.record())
        result = self.bot.wait_for_submit_result("21:00-22:30")
        self.assertEqual(result.status, "confirmed")

    def test_slot_popup_is_not_success(self):
        self.page.set_content('<div class="el-message">已预约 点击查看详情</div>')
        result = self.bot.wait_for_submit_result("21:00-22:30", timeout_ms=100)
        self.assertEqual(result.status, "unknown")

    def test_record_must_match_date_venue_and_active_status(self):
        cases = [
            self.record(date="2026-09-17"),
            self.record(venue="另一个场馆"),
            self.record(status="已取消"),
            self.record(slot="20:00-21:00"),
            self.record(date="2026-09-20", status="已预约 待签到 申请时间 2026-09-18"),
        ]
        for html in cases:
            with self.subTest(html=html):
                self.page.set_content(html)
                self.assertEqual(self.bot.confirmed_appointment_record_text("21:00-22:30"), "")

    def test_cannot_combine_status_from_separate_records(self):
        self.page.set_content('<div class="record-list">' +
                              self.record(status="已取消") +
                              self.record(date="2026-09-17") + '</div>')
        self.assertEqual(self.bot.confirmed_appointment_record_text("21:00-22:30"), "")

    def test_late_table_load_waits_without_reload(self):
        self.page.set_content('<div id="schedule">加载中</div>')
        self.page.evaluate("html => setTimeout(() => schedule.innerHTML = html, 4000)", self.calendar())
        self.assertIsNotNone(self.bot.ensure_target_date_visible())

    def test_hidden_duplicate_date_does_not_mask_visible_header(self):
        self.page.set_content('<span style="display:none">2026-09-18</span>' + self.calendar())
        self.assertIsNotNone(self.bot.ensure_target_date_visible(timeout_ms=500))

    def test_week_turn_waits_for_changed_headers(self):
        self.page.set_content('<div id="schedule">' + self.calendar('2026-09-13') + '</div>' + '''
            <button onclick="window.turns++; setTimeout(() => schedule.innerHTML = window.nextWeek, 700)">后一周</button>
            <script>window.turns = 0</script>''')
        self.page.evaluate('html => window.nextWeek = html', self.calendar())
        self.bot.click_first_text = lambda labels, **kwargs: (self.page.get_by_text(labels[0]).click() or True)
        self.bot.wait_network_idle = Mock()
        self.assertIsNotNone(self.bot.ensure_target_date_visible(timeout_ms=2500))
        self.assertEqual(self.page.evaluate("window.turns"), 1)

    def test_full_cell_is_never_clicked(self):
        self.page.set_content('''<table><tr><th>时段</th><th>2026-09-18</th></tr>
            <tr><td>21:00-22:30</td><td onclick="window.clicked=true">约满 (15/15)</td></tr></table>''')
        self.assertEqual(self.bot.click_slot(self.page, "21:00-22:30")["status"], "unavailable")
        self.assertIsNone(self.page.evaluate("window.clicked"))

    def test_dry_run_does_not_click_available_cell(self):
        self.bot.dry_run = True
        self.page.set_content('''<table><tr><th>时段</th><th>2026-09-18</th></tr>
            <tr><td>21:00-22:30</td><td onclick="window.clicked=true">可预约 (8/15)</td></tr></table>''')
        self.assertEqual(self.bot.click_slot(self.page, "21:00-22:30")["status"], "would-click")
        self.assertIsNone(self.page.evaluate("window.clicked"))

    def test_calendar_snapshot_and_selection_use_date_and_row(self):
        self.page.set_content(self.calendar('2026-09-17') + self.calendar())
        snapshot = self.bot.read_slots(self.page)
        self.assertEqual(snapshot['21:00-22:30']['status'], 'available')
        self.assertEqual(snapshot['20:00-21:00']['status'], 'unavailable')
        self.assertIsNone(self.page.evaluate('window.clicked'))
        self.page.locator('.reservation_data').last.locator('dd').first.evaluate(
            "e => e.onclick = () => window.clicked = 'target-date'"
        )
        self.assertEqual(self.bot.click_slot(self.page, '21:00-22:30')['status'], 'clicked')
        self.assertEqual(self.page.evaluate('window.clicked'), 'target-date')

    def test_availability_is_rechecked_before_click(self):
        self.page.set_content(self.calendar())
        self.assertEqual(self.bot.read_slots(self.page)['21:00-22:30']['status'], 'available')
        self.page.locator('.can_active').evaluate("e => e.textContent = '约满 (15/15)'")
        self.assertEqual(self.bot.click_slot(self.page, '21:00-22:30')['status'], 'unavailable')
        self.assertIsNone(self.page.evaluate('window.clicked'))

    def test_loading_overlay_blocks_selection(self):
        self.page.set_content(self.calendar() + '<div class="el-loading-mask">加载中</div>')
        self.assertEqual(self.bot.click_slot(self.page, '21:00-22:30')['status'], 'not-ready')
        self.assertIsNone(self.page.evaluate('window.clicked'))

    def test_unknown_layout_never_guesses_coordinates(self):
        self.page.set_content('<div>2026-09-18</div><div>21:00-22:30</div><button>可预约</button>')
        self.assertEqual(self.bot.click_slot(self.page, '21:00-22:30')['status'], 'not-found')

    def test_fast_success_does_not_wait_for_nonexistent_dialog(self):
        self.page.set_content('<button onclick="records.innerHTML = window.recordHtml">确认预约</button>'
                              '<div id="records"></div>')
        self.page.evaluate('html => window.recordHtml = html', self.record())
        self.bot.confirm_submit_if_needed = Mock()
        self.assertEqual(self.bot.submit_booking('21:00-22:30').status, 'confirmed')
        self.bot.confirm_submit_if_needed.assert_not_called()

    def test_late_confirmation_dialog_is_clicked_once(self):
        self.page.set_content('<div id="records"></div><div id="dialog"></div><script>window.confirms=0</script>')
        self.page.evaluate('html => window.recordHtml = html', self.record())
        self.page.evaluate('''() => setTimeout(() => dialog.innerHTML =
            `<div role="dialog">确认提交预约？<button onclick="window.confirms++;
            records.innerHTML=window.recordHtml; dialog.innerHTML=''">确定</button></div>`, 150)''')
        result = self.bot.wait_for_submit_result('21:00-22:30', confirm_dialog=True)
        self.assertEqual(result.status, 'confirmed')
        self.assertEqual(self.page.evaluate('window.confirms'), 1)

    def test_confirmation_does_not_click_unrelated_button(self):
        self.page.set_content('<button onclick="window.clicked=true">确定</button>')
        self.assertFalse(self.bot.confirm_submit_if_needed(timeout_ms=0))
        self.assertIsNone(self.page.evaluate('window.clicked'))

    def test_reading_notice_is_dismissed_on_single_check(self):
        self.page.set_content('<div role="dialog">阅读须知<button onclick="window.clicked=true">确定</button></div>')
        self.assertTrue(self.bot.dismiss_reading_notice(timeout_ms=0))
        self.assertTrue(self.page.evaluate('window.clicked'))

    def test_pending_submission_is_confirmed_from_my_appointments(self):
        self.page.set_content('<button onclick="document.getElementById(\'records\').hidden=false">我的预约</button>'
                              '<div id="records" hidden>' + self.record() + '</div>')
        self.bot.pending_slots.add("21:00-22:30")
        self.assertEqual(self.bot.reconcile_pending_slots([]), ["21:00-22:30"])
        self.assertEqual(self.bot.pending_slots, set())

    def detached_frame(self):
        self.page.evaluate("() => {const f=document.createElement('iframe'); f.id='old-frame'; document.body.append(f);}")
        frame = self.page.locator('#old-frame').element_handle().content_frame()
        self.page.locator('#old-frame').evaluate('e => e.remove()')
        self.assertTrue(frame.is_detached())
        return frame

    def test_detached_frame_during_locator_all_is_skipped(self):
        self.page.set_content(self.calendar())
        frame = self.detached_frame()
        # Reproduce a frame disappearing AFTER scopes() takes its snapshot.
        self.bot.scopes = Mock(return_value=[frame, self.page.main_frame])
        self.assertEqual(self.bot.scope_with_text('2026-09-18'), self.page.main_frame)

    def test_calendar_wait_reacquires_frame_after_detach(self):
        self.page.set_content(self.calendar())
        frame = self.detached_frame()
        self.bot.scope_with_text = Mock(side_effect=[frame, self.page.main_frame])
        self.assertEqual(self.bot.ensure_target_date_visible(timeout_ms=1000), self.page.main_frame)

    def test_dead_frame_read_is_not_ready_instead_of_empty_or_full(self):
        self.page.set_content(self.calendar())
        frame = self.detached_frame()
        self.assertTrue(all(item['status'] == 'not-ready' for item in self.bot.read_slots(frame).values()))

    def test_calendar_click_uses_browser_input_events(self):
        self.page.set_content(self.calendar())
        self.page.locator('.can_active').evaluate('e => e.onclick = event => window.trustedClick = event.isTrusted')
        self.assertEqual(self.bot.click_slot(self.page, '21:00-22:30')['status'], 'clicked')
        self.assertTrue(self.page.evaluate('window.trustedClick'))

    def test_submit_navigation_error_is_unknown_and_not_reclicked(self):
        self.page.set_content('<button>确认预约</button>')
        self.bot.click_submit_button = Mock(side_effect=PlaywrightError('Execution context was destroyed'))
        result = self.bot.submit_booking('21:00-22:30')
        self.assertEqual(result.status, 'unknown')
        self.bot.click_submit_button.assert_called_once()

    def test_submit_click_timeout_is_unknown_and_not_reclicked(self):
        self.page.set_content('<button>确认预约</button>')
        self.bot.click_submit_button = Mock(side_effect=PlaywrightTimeoutError('click timed out'))
        self.assertEqual(self.bot.submit_booking('21:00-22:30').status, 'unknown')
        self.bot.click_submit_button.assert_called_once()


if __name__ == "__main__":
    unittest.main()
