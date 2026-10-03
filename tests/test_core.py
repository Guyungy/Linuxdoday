import ast
import contextlib
import io
import json
import os
import py_compile
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import hot_topics
import linux_do_scraper as scraper
import push_to_feishu as feishu
import daily_report as report
import service


REPO_ROOT = Path(__file__).resolve().parent.parent


def fake_browser_utils():
    """替代 browser_utils：真实的那个 import playwright，CI 里没装。"""
    return SimpleNamespace(
        start_browser=lambda **kwargs: (SimpleNamespace(close=lambda: None), SimpleNamespace()),
        check_login=lambda page, timeout=45: True,
        wait_json_ready=lambda page, slug="develop/4", timeout=90, interval=3: True,
    )


class ScraperTests(unittest.TestCase):
    def test_browser_uses_full_category_path(self):
        class Page:
            def evaluate(self, script, args):
                self.assert_path(args)
                return {"topic_list": {"topics": []}}

            def assert_path(self, args):
                self.seen_path = args["categoryPath"]

        page = Page()
        with patch.object(scraper.time, "sleep"):
            scraper.scrape_category(page, {"n": "网盘资源", "u": "/c/resource/cloud-asset/94"}, page_delay=(0, 0))
        self.assertEqual("/c/resource/cloud-asset/94", page.seen_path)

    def test_rss_retries_network_errors(self):
        feed = b"<rss><channel><item><link>https://linux.do/t/topic/123</link><title>test</title></item></channel></rss>"
        session = SimpleNamespace(get=unittest.mock.Mock(side_effect=[OSError("offline"), SimpleNamespace(status_code=200, content=feed)]))
        with patch.dict("sys.modules", {"curl_cffi": SimpleNamespace(requests=SimpleNamespace())}), patch.object(scraper.time, "sleep"):
            rows = scraper.scrape_category_rss({"u": "/c/develop/4"}, session=session)
        self.assertEqual(["123"], [row["id"] for row in rows])
        self.assertEqual(2, session.get.call_count)

    def test_rss_rejects_non_feed_response(self):
        session = SimpleNamespace(get=lambda *args, **kwargs: SimpleNamespace(status_code=200, content=b"<html></html>"))
        with patch.dict("sys.modules", {"curl_cffi": SimpleNamespace(requests=SimpleNamespace())}):
            with self.assertRaisesRegex(RuntimeError, "不是有效的 RSS"):
                scraper.scrape_category_rss({"u": "/c/develop/4"}, session=session)

    def test_rss_category_failure_is_not_reported_as_success(self):
        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

        fake_curl = SimpleNamespace(requests=SimpleNamespace(Session=Session))
        categories = [{"n": "A", "u": "/c/a/1"}, {"n": "B", "u": "/c/b/2"}]
        with patch.dict("sys.modules", {"curl_cffi": fake_curl}), patch.object(scraper, "scrape_category_rss", side_effect=[[], RuntimeError("HTTP 429")]), patch.object(scraper.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "B 第1页: HTTP 429"):
                scraper.scrape_all_rss(categories)

    def test_flatten_deduplicates_topics_across_categories(self):
        result = {
            "A": [{"id": "1", "title": "first"}],
            "B": [{"id": "1", "title": "duplicate"}, {"id": "2", "title": "second"}],
        }
        rows = scraper.flatten(result)
        self.assertEqual(["1", "2"], [row["id"] for row in rows])
        self.assertEqual("A", rows[0]["category"])

    def test_incremental_merge_keeps_old_rich_metrics_for_rss(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "topics.json"
            cache.write_text(json.dumps([{
                "id": "1", "title": "old", "views": 99, "replies": 8, "tags": ["manual"]
            }]), encoding="utf-8")
            incoming = [{
                "id": "1", "title": "new", "views": 0, "replies": 0,
                "tags": ["auto"], "_rss_source": True,
            }]
            with patch.object(scraper, "CACHE_FILE", str(cache)), patch.object(scraper, "DATA_DIR", directory):
                new_rows, merged = scraper.merge_incremental(incoming)
            self.assertEqual([], new_rows)
            self.assertEqual(99, merged[0]["views"])
            self.assertEqual(["manual", "auto"], merged[0]["tags"])


class PersistenceTests(unittest.TestCase):
    def test_service_atomic_json_write(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "state.json"
            service.atomic_write_json(target, {"ok": True})
            self.assertEqual({"ok": True}, json.loads(target.read_text(encoding="utf-8")))

    def test_feishu_id_cache_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "ids.json"
            with patch.object(feishu, "CACHE_FILE", target), patch.object(feishu, "CACHE_TTL", 3600):
                feishu.save_id_cache({"2", "1"})
                self.assertEqual({"1", "2"}, feishu.load_id_cache())


class ServiceModeTests(unittest.TestCase):
    def build(self, **env):
        with patch.dict(os.environ, env):
            return service.ScrapeService()

    def test_default_mode_scrapes_via_rss(self):
        command = self.build(SCRAPE_MODE="", SCRAPE_RSS_PAGES="1").command()
        self.assertIn("--scrape", command)
        self.assertIn("--rss", command)
        self.assertNotIn("--rss-pages", command)

    def test_rss_mode_keeps_paging_flag(self):
        command = self.build(SCRAPE_MODE="rss", SCRAPE_RSS_PAGES="3").command()
        self.assertIn("--rss", command)
        self.assertIn("--rss-pages", command)

    def test_browser_mode_drops_rss_flags(self):
        command = self.build(SCRAPE_MODE="browser", SCRAPE_RSS_PAGES="3").command()
        self.assertIn("--scrape", command)
        self.assertNotIn("--rss", command)
        self.assertNotIn("--rss-pages", command)
        self.assertNotIn("--headless", command)

    def test_browser_mode_passes_depth(self):
        command = self.build(SCRAPE_MODE="browser", SCRAPE_MAX_PAGES="10").command()
        self.assertIn("--max-pages", command)
        self.assertIn("10", command)

    def test_rss_mode_ignores_depth(self):
        command = self.build(SCRAPE_MODE="rss", SCRAPE_MAX_PAGES="10").command()
        self.assertNotIn("--max-pages", command)

    def test_content_flags(self):
        command = self.build(SCRAPE_MODE="browser", SCRAPE_CONTENT="true", SCRAPE_CONTENT_LIMIT="300").command()
        self.assertIn("--content", command)
        self.assertIn("--content-limit", command)
        self.assertIn("300", command)

    def test_content_off_by_default(self):
        self.assertNotIn("--content", self.build(SCRAPE_MODE="browser").command())

    def test_hot_enabled_by_default_in_browser_mode(self):
        command = self.build(SCRAPE_MODE="browser").command()
        self.assertIn("--hot", command)

    def test_hot_can_be_disabled(self):
        command = self.build(SCRAPE_MODE="browser", SCRAPE_HOT="false").command()
        self.assertNotIn("--hot", command)

    def test_hot_not_used_in_rss_mode(self):
        self.assertNotIn("--hot", self.build(SCRAPE_MODE="rss", SCRAPE_HOT="true").command())

    def test_hot_method_is_not_shadowed_by_flag(self):
        svc = self.build(SCRAPE_MODE="browser")
        self.assertIsInstance(svc.hot_enabled, bool)
        self.assertTrue(callable(svc.hot))
        self.assertIn("daily", svc.hot())

    def test_wait_until_returns_immediately_when_due_in_past(self):
        svc = self.build(SCRAPE_MODE="browser")
        due = datetime.now(timezone.utc) - timedelta(seconds=5)
        started = time.monotonic()
        self.assertTrue(svc.wait_until(due))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_wait_until_stops_on_stop_event(self):
        svc = self.build(SCRAPE_MODE="browser")
        svc.stop_event.set()
        due = datetime.now(timezone.utc) + timedelta(hours=6)
        self.assertFalse(svc.wait_until(due, tick=5))

    def test_startup_run_skipped_when_recent_success(self):
        with tempfile.TemporaryDirectory() as directory:
            latest = Path(directory) / "latest_run.json"
            latest.write_text(json.dumps({
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")
            }), encoding="utf-8")
            svc = self.build(SCRAPE_MODE="browser")
            with patch.object(service, "LATEST_FILE", latest):
                self.assertFalse(svc.should_run_on_start())

    def test_startup_run_when_history_is_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            latest = Path(directory) / "latest_run.json"
            old = datetime.now(timezone.utc) - timedelta(seconds=99999)
            latest.write_text(json.dumps({"generated_at": old.isoformat(timespec="seconds")}), encoding="utf-8")
            svc = self.build(SCRAPE_MODE="browser")
            with patch.object(service, "LATEST_FILE", latest):
                self.assertTrue(svc.should_run_on_start())

    def test_startup_run_when_no_history(self):
        with tempfile.TemporaryDirectory() as directory:
            svc = self.build(SCRAPE_MODE="browser")
            with patch.object(service, "LATEST_FILE", Path(directory) / "missing.json"):
                self.assertTrue(svc.should_run_on_start())

    def test_status_seeded_from_last_run_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            latest = Path(directory) / "latest_run.json"
            latest.write_text(json.dumps({
                "generated_at": "2026-09-29T01:24:10+00:00", "count": 81,
            }), encoding="utf-8")
            with patch.object(service, "LATEST_FILE", latest):
                snapshot = self.build(SCRAPE_MODE="browser").snapshot()
            self.assertEqual("2026-09-29T01:24:10+00:00", snapshot["last_success_at"])
            self.assertEqual(81, snapshot["last_new_topics"])

    def test_daily_report_disabled_by_default(self):
        svc = self.build(SCRAPE_MODE="browser")
        self.assertFalse(svc.daily_report)
        self.assertFalse(svc.should_report_today())

    def test_daily_report_command_carries_chat_and_model(self):
        svc = self.build(DAILY_REPORT="true", DAILY_REPORT_CHAT="oc_abc", DAILY_REPORT_MODEL="glm-5.3-flash")
        command = svc.daily_report_command()
        self.assertIn("--push-chat", command)
        self.assertIn("oc_abc", command)
        self.assertIn("--model", command)

    def test_daily_report_uses_card_by_default(self):
        command = self.build(DAILY_REPORT="true", DAILY_REPORT_CHAT="oc_abc").daily_report_command()
        self.assertIn("--card", command)

    def test_daily_report_card_can_be_turned_off(self):
        command = self.build(DAILY_REPORT="true", DAILY_REPORT_CHAT="oc_abc",
                             DAILY_REPORT_CARD="false").daily_report_command()
        self.assertNotIn("--card", command)

    def test_daily_report_skipped_before_hour(self):
        svc = self.build(DAILY_REPORT="true", DAILY_REPORT_HOUR="23")
        with patch.object(service, "datetime") as fake:
            fake.now.return_value = datetime(2026, 9, 29, 9, 0, 0)
            self.assertFalse(svc.should_report_today())

    def test_daily_report_skipped_when_file_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            svc = self.build(DAILY_REPORT="true", DAILY_REPORT_HOUR="0")
            report_dir = Path(directory)
            (report_dir / f"AI日报-{date.today()}.md").write_text("x", encoding="utf-8")
            with patch.object(service, "REPORT_DIR", report_dir):
                self.assertFalse(svc.should_report_today())

    def test_unknown_mode_falls_back_to_rss(self):
        self.assertEqual("rss", self.build(SCRAPE_MODE="nope").mode)


class DailyReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.cache = root / "topics.json"
        self.contents = root / "content.json"
        self.hot = root / "hot.json"
        self.cache.write_text(json.dumps([
            {"id": "1", "title": "Sonnet 5.5 上线", "category": "前沿快讯", "author": "a",
             "views": 1500, "replies": 10, "like_count": 5, "created_at": "2026-09-29T02:00:00.000Z"},
            {"id": "2", "title": "旧帖", "category": "搞七捻三", "author": "b",
             "views": 100, "replies": 1, "like_count": 0, "created_at": "2026-09-27T02:00:00.000Z"},
        ]), encoding="utf-8")
        self.contents.write_text(json.dumps({"1": {"content": "Sonnet 5.5 实测正文"}}), encoding="utf-8")
        self.hot.write_text(json.dumps({"daily": [], "weekly": []}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def collect(self, days=3):
        with patch.object(report, "CACHE_FILE", str(self.cache)), \
             patch.object(report, "CONTENT_FILE", str(self.contents)), \
             patch.object(report, "HOT_FILE", str(self.hot)):
            return report.collect(date(2026, 9, 29), days)

    def test_collect_filters_to_the_requested_day(self):
        rows, overview, _ = self.collect(days=1)
        self.assertEqual(["1"], [r["id"] for r in rows])
        self.assertEqual(1, overview["posts"])
        self.assertEqual(1, overview["with_content"])
        self.assertEqual(1500, overview["views"])

    def test_three_day_window_includes_older_posts(self):
        rows, overview, _ = self.collect(days=3)
        self.assertEqual({"1", "2"}, {r["id"] for r in rows})
        self.assertEqual("2026-09-27", overview["start"])
        self.assertEqual("2026-09-29", overview["end"])
        self.assertEqual(3, overview["days"])

    def test_previous_window_is_counted_for_comparison(self):
        rows = [
            {"id": "1", "title": "今天", "category": "A", "views": 10, "replies": 0,
             "like_count": 0, "created_at": "2026-09-29T02:00:00.000Z"},
            {"id": "old", "title": "上一窗口", "category": "A", "views": 10, "replies": 0,
             "like_count": 0, "created_at": "2026-09-25T02:00:00.000Z"},
        ]
        self.cache.write_text(json.dumps(rows), encoding="utf-8")
        _, overview, _ = self.collect(days=3)
        self.assertEqual(1, overview["posts"])
        self.assertEqual(1, overview["prev_posts"])

    def test_render_topics_groups_by_theme_with_representatives(self):
        rows, overview, hot = self.collect()
        text = report.render_topics(rows, overview)
        self.assertIn("## 📌 话题汇总", text)
        self.assertIn("模型版本/发布", text)
        self.assertIn("Sonnet 5.5 上线", text)
        self.assertIn("https://linux.do/t/topic/1", text)

    def test_render_hot_lists_ranked_posts(self):
        rows, overview, hot = self.collect()
        text = report.render_hot(rows, hot, 30)
        self.assertIn("其它高热帖", text)
        self.assertIn("1. [", text)

    def test_report_has_no_data_overview_section(self):
        rows, overview, hot = self.collect()
        self.assertNotIn("数据概览", report.render_topics(rows, overview))
        self.assertNotIn("参与度分层", report.render_hot(rows, hot, 30))

    def test_to_message_converts_headings_and_tables(self):
        markdown = (
            "# 标题\n\n## 热帖榜\n\n"
            "| # | 板块 | 标题 | 👁 |\n|---|---|---|---|\n"
            "| 1 | 开发调优 | [某帖](https://linux.do/t/topic/1) | 5 |\n"
        )
        message = report.to_message(markdown)
        self.assertIn("**热帖榜**", message)
        self.assertIn("1. [某帖](https://linux.do/t/topic/1)", message)
        self.assertNotIn("|", message)

    def test_themes_are_detected(self):
        rows, overview, _ = self.collect()
        names = {t["name"]: t["posts"] for t in overview["themes"]}
        self.assertEqual(1, names.get("模型版本/发布"))


class ZeroRowDetectionTests(unittest.TestCase):
    """P1：0 条结果不得被判为成功 —— browser 路径此前没有任何等价断言。"""

    def run_scrape(self, argv, **mocked):
        stack = [
            patch.dict("sys.modules", {"browser_utils": fake_browser_utils()}),
            patch.object(sys, "argv", argv),
            patch.object(scraper, "start_browser",
                         return_value=(SimpleNamespace(close=lambda: None), SimpleNamespace())),
            patch.object(scraper, "check_login", return_value=True),
        ]
        for name, value in mocked.items():
            stack.append(patch.object(scraper, name, return_value=value))
        for item in stack:
            item.start()
        try:
            # main() 会把飞书行打到 stdout；测试只关心退出码，别污染测试输出。
            with contextlib.redirect_stdout(io.StringIO()):
                scraper.main()
        finally:
            for item in reversed(stack):
                item.stop()

    def test_browser_zero_rows_is_not_reported_as_success(self):
        """对照 test_rss_category_failure_is_not_reported_as_success 的 browser 版。"""
        with self.assertRaisesRegex(RuntimeError, "0 条"):
            scraper.assert_rows_present([], "browser")
        self.assertEqual([{"id": "1"}], scraper.assert_rows_present([{"id": "1"}], "browser"))

    def test_browser_pipeline_exits_nonzero_on_zero_rows(self):
        with patch.object(scraper.time, "sleep"):
            with self.assertRaises(SystemExit) as caught:
                self.run_scrape(
                    ["linux_do_scraper.py", "--scrape", "--max-pages", "1", "--no-proxy"],
                    scrape_all={},
                )
        self.assertEqual(1, caught.exception.code)

    def test_rss_pipeline_exits_nonzero_on_zero_rows(self):
        # RSS 已有「任一板块失败即 raise」，但「全部板块都返回 0 条」这条缝隙同样要堵。
        with self.assertRaises(SystemExit) as caught:
            self.run_scrape(["linux_do_scraper.py", "--scrape", "--rss", "--no-proxy"],
                            scrape_all_rss={})
        self.assertEqual(1, caught.exception.code)

    def test_board_empty_via_json_and_dom_leaves_a_warning(self):
        """P1 的源头：JSON 失败 + DOM 也没有数据时，必须留下 WARN 现场。"""
        page = SimpleNamespace(evaluate=unittest.mock.Mock(side_effect=[{"error": "HTTP 403"}] * 2))
        stderr = io.StringIO()
        with patch.object(scraper.time, "sleep"), \
             patch.object(scraper, "scrape_category_dom", return_value=[]), \
             contextlib.redirect_stderr(stderr):
            rows = scraper.scrape_category(page, {"n": "开发调优", "u": "/c/develop/4"},
                                           page_delay=(0, 0))
        self.assertEqual([], rows)
        self.assertIn(scraper.WARN_PREFIX, stderr.getvalue())
        self.assertIn("CF 挑战", stderr.getvalue())

    def test_rows_present_does_not_trip_the_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "topics.json"
            with patch.object(scraper, "DATA_DIR", directory), \
                 patch.object(scraper, "CACHE_FILE", str(cache)):
                with self.assertRaises(SystemExit) as caught:
                    self.run_scrape(
                        ["linux_do_scraper.py", "--scrape", "--max-pages", "1", "--no-proxy"],
                        scrape_all={"开发调优": [{"id": "1", "title": "正常一帖"}]},
                    )
                self.assertEqual(0, caught.exception.code)
                self.assertTrue(cache.exists())


class WarningSurfacingTests(unittest.TestCase):
    """P1：热榜/正文的 except 不再静默，现场必须出现在 /status。"""

    def build(self, **env):
        with patch.dict(os.environ, env):
            return service.ScrapeService()

    def test_degraded_fetch_reaches_last_error_and_keeps_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            latest = Path(directory) / "latest_run.json"
            svc = self.build(SCRAPE_MODE="browser")
            stderr = (
                "[09:00:01] 启动浏览器（proxy=无，离屏）...\n"
                "[09:00:02] WARN: 热榜全部周期抓取失败（2/2），保留上一份榜单不覆盖: [daily] 返回 0 条\n"
                "[09:00:03] WARN: 新帖正文抓取失败（列表已入库，正文缺失）: TimeoutError('x')\n"
                "[09:00:04] 共抓取 3 条帖子\n"
            )
            result = SimpleNamespace(returncode=0, stdout=json.dumps([{"Topic ID": "1"}]), stderr=stderr)
            with patch.object(service, "LATEST_FILE", latest), \
                 patch.object(service, "STDERR_LOG_FILE", Path(directory) / "scrape_stderr.log"), \
                 patch.object(service, "run_with_wall_clock_deadline", return_value=result):
                ok, _ = svc.run_once("test")
            snapshot = svc.snapshot()

        self.assertTrue(ok)
        self.assertIsNotNone(snapshot["last_success_at"])
        self.assertIn("热榜全部周期抓取失败", snapshot["last_error"])
        self.assertIn("新帖正文抓取失败", snapshot["last_error"])
        self.assertNotIn("WARN:", snapshot["last_error"])       # 前缀已剥离，只留现场
        self.assertIn("启动浏览器", snapshot["last_stderr"])     # 成功分支不再整段丢弃 stderr

    def test_clean_run_leaves_last_error_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            latest = Path(directory) / "latest_run.json"
            svc = self.build(SCRAPE_MODE="rss")
            result = SimpleNamespace(returncode=0, stdout=json.dumps([{"Topic ID": "1"}]),
                                     stderr="[09:00:01] 共抓取 1 条帖子\n")
            with patch.object(service, "LATEST_FILE", latest), \
                 patch.object(service, "STDERR_LOG_FILE", Path(directory) / "scrape_stderr.log"), \
                 patch.object(service, "run_with_wall_clock_deadline", return_value=result):
                ok, _ = svc.run_once("test")
            snapshot = svc.snapshot()

        self.assertTrue(ok)
        self.assertIsNone(snapshot["last_error"])
        self.assertIn("共抓取 1 条帖子", snapshot["last_stderr"])

    def test_hot_collect_records_empty_periods_as_failures(self):
        page = SimpleNamespace(evaluate=lambda script, period: {"topics": []})
        with patch.object(hot_topics, "load_category_map", return_value={}):
            payload = hot_topics.collect(page, ("daily", "weekly"))
        self.assertEqual([], payload["daily"])
        self.assertEqual(2, len(payload["errors"]))
        self.assertFalse(payload["partial"])

    def test_hot_collect_records_http_failure(self):
        page = SimpleNamespace(evaluate=lambda script, period: {"error": 403})
        with patch.object(hot_topics, "load_category_map", return_value={}):
            payload = hot_topics.collect(page, ("daily", "weekly"))
        self.assertEqual(2, len(payload["errors"]))
        self.assertIn("403", payload["errors"][0])
        self.assertFalse(payload["partial"])

    def test_hot_collect_marks_partial_when_one_period_fails(self):
        def evaluate(script, period):
            if period == "daily":
                return {"topics": [{"id": 1, "title": "t", "slug": "s", "posters": []}]}
            return {"topics": []}

        page = SimpleNamespace(evaluate=evaluate)
        with patch.object(hot_topics, "load_category_map", return_value={}):
            payload = hot_topics.collect(page, ("daily", "weekly"))
        self.assertEqual(1, len(payload["daily"]))
        self.assertTrue(payload["partial"])
        self.assertEqual(1, len(payload["errors"]))

    def test_hot_topics_main_keeps_previous_file_and_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            hot_file = Path(directory) / "hot_topics.json"
            good = {"daily": [{"id": "1"}], "weekly": [{"id": "2"}]}
            hot_file.write_text(json.dumps(good), encoding="utf-8")
            payload = {"generated_at": "x", "daily": [], "weekly": [],
                       "errors": ["[daily] 返回 0 条", "[weekly] 返回 0 条"]}
            with patch.dict("sys.modules", {"browser_utils": fake_browser_utils()}), \
                 patch.object(hot_topics, "HOT_FILE", str(hot_file)), \
                 patch.object(hot_topics, "collect", return_value=payload), \
                 patch.object(sys, "argv", ["hot_topics.py", "--no-proxy"]):
                with self.assertRaises(SystemExit) as caught:
                    hot_topics.main()
            self.assertEqual(1, caught.exception.code)
            self.assertEqual(good, json.loads(hot_file.read_text(encoding="utf-8")))

    def test_hot_topics_main_still_saves_on_partial_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            hot_file = Path(directory) / "hot_topics.json"
            payload = {"generated_at": "x", "daily": [{"id": "1"}], "weekly": [],
                       "errors": ["[weekly] 返回 0 条"], "partial": True}
            with patch.dict("sys.modules", {"browser_utils": fake_browser_utils()}), \
                 patch.object(hot_topics, "HOT_FILE", str(hot_file)), \
                 patch.object(hot_topics, "collect", return_value=payload), \
                 patch.object(sys, "argv", ["hot_topics.py", "--no-proxy", "--json"]):
                with contextlib.redirect_stdout(io.StringIO()):
                    hot_topics.main()
            self.assertEqual(payload, json.loads(hot_file.read_text(encoding="utf-8")))


class SchedulerResilienceTests(unittest.TestCase):
    """P2：单轮异常不得打死调度线程，且失败必须可见。"""

    def build(self, **env):
        with patch.dict(os.environ, env):
            return service.ScrapeService()

    def test_missing_daily_report_script_is_visible_not_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            svc = self.build(SCRAPE_MODE="rss", DAILY_REPORT="true", DAILY_REPORT_HOUR="0")

            def fake_run(command, timeout, **kwargs):
                if str(command[1]).endswith("daily_report.py"):
                    # 镜像里没 COPY daily_report.py 时的等价场景
                    raise FileNotFoundError(2, "No such file or directory", "daily_report.py")
                return SimpleNamespace(returncode=0, stdout=json.dumps([{"Topic ID": "1"}]), stderr="")

            with patch.object(service, "LATEST_FILE", root / "latest_run.json"), \
                 patch.object(service, "STDERR_LOG_FILE", root / "scrape_stderr.log"), \
                 patch.object(service, "DATA_DIR", root), \
                 patch.object(service, "REPORT_DIR", root / "reports"), \
                 patch.object(service, "run_with_wall_clock_deadline", side_effect=fake_run):
                ok, _ = svc.run_once("schedule")

            snapshot = svc.snapshot()

        self.assertTrue(ok)                                    # 抓取本身成功
        self.assertEqual("idle", snapshot["status"])           # 没被日报失败带崩
        self.assertIsNotNone(snapshot["last_success_at"])
        self.assertIn("daily_report.py", snapshot["last_error"])

    def test_scheduler_survives_repeated_run_once_exceptions(self):
        svc = self.build(SCRAPE_MODE="rss")
        rounds = []

        def boom(trigger):
            rounds.append(trigger)
            if len(rounds) >= 3:
                svc.stop_event.set()
            raise FileNotFoundError(2, "No such file or directory", "daily_report.py")

        with patch.object(svc, "should_run_on_start", return_value=True), \
             patch.object(svc, "run_once", side_effect=boom), \
             patch.object(svc, "wait_until",
                          side_effect=lambda due, tick=30: not svc.stop_event.is_set()):
            svc.scheduler()          # 异常若逃逸出调度循环，这里会直接抛 → 测试失败

        snapshot = svc.snapshot()
        self.assertEqual(3, len(rounds))                       # 连炸三次仍在继续调度
        self.assertEqual(3, snapshot["runs"])
        self.assertEqual("error", snapshot["status"])
        self.assertIn("FileNotFoundError", snapshot["last_error"])
        self.assertIn("调度线程继续运行", snapshot["last_error"])
        self.assertIsNone(snapshot["next_run_at"])             # 退出时清理干净


class WallClockDeadlineTests(unittest.TestCase):
    """POLL-78：轮次时限改墙钟口径。

    macOS 合盖休眠会冻结单调钟，subprocess.run(timeout=) 因此永远够不到上限
    （2026-09-29T20:11Z 那轮墙钟跑了 5h32m54s，进程内只累计 123s）。下面用
    「把 subprocess 的单调钟钉死」在进程内等价复现这个冻结。
    """

    @staticmethod
    def freeze_monotonic():
        # subprocess 在 import 时就把 time.monotonic 绑成了 subprocess._time，
        # 改 time.monotonic 打不到它，必须改这个绑定。
        return patch.object(service.subprocess, "_time", lambda: 0.0)

    def test_frozen_monotonic_defeats_plain_run_timeout(self):
        """改前的跑法：单调钟冻住时 0.5s 的上限形同虚设。"""
        child = [sys.executable, "-c", "import time; time.sleep(2)"]
        started = time.time()
        with self.freeze_monotonic():
            completed = service.subprocess.run(child, capture_output=True, text=True, timeout=0.5)
        self.assertEqual(0, completed.returncode)          # 没被 timeout 拦下
        self.assertGreater(time.time() - started, 1.5)     # 老老实实等完了 2s

    def test_wall_clock_deadline_fires_under_the_same_freeze(self):
        """改后的跑法：同样的冻结下照常到点 kill（改前没有这个函数，直接失败）。"""
        child = [sys.executable, "-c", "import time; time.sleep(30)"]
        with self.freeze_monotonic():
            started = time.time()
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                service.run_with_wall_clock_deadline(child, timeout=1, tick=0.2)
            elapsed = time.time() - started
        self.assertEqual(1, caught.exception.timeout)
        self.assertLess(elapsed, 5)

    def test_wall_clock_deadline_judges_on_wake_instead_of_waiting_again(self):
        """合盖 → 唤醒：墙钟一次跳了几千秒，第一眼就判定超时，不按剩余时间续命。"""
        child = [sys.executable, "-c", "import time; time.sleep(10)"]
        real_time = time.time
        started = real_time()
        awake = {"still_dreaming": True}

        def jumped():
            if awake["still_dreaming"] and real_time() - started > 0.3:
                awake["still_dreaming"] = False     # 这一刻唤醒，墙钟一次跳掉 4000 秒
            return real_time() if awake["still_dreaming"] else real_time() + 4000

        with patch.object(service.time, "time", jumped):
            with self.assertRaises(subprocess.TimeoutExpired):
                service.run_with_wall_clock_deadline(child, timeout=60, tick=0.2)
        self.assertLess(real_time() - started, 5)


class RoundTimeoutBehaviourTests(unittest.TestCase):
    """POLL-78 验收 2：到点 kill、last_error 文案、runs +1；追加交付物：完整 stderr 落盘。"""

    def build(self, **env):
        with patch.dict(os.environ, env):
            return service.ScrapeService()

    def test_round_times_out_on_wall_clock_and_kills_the_child(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "child-survived.txt"
            svc = self.build(SCRAPE_MODE="rss")
            svc.timeout = 1          # 配置下限是 60s，测试里直接改属性
            log_file = root / "scrape_stderr.log"
            # 子进程先吐一段长 stderr（超过 /status 的 2000 字符尾部上限）再挂住，
            # 跑完 30 秒才写 marker —— 到点没被 kill 的话 marker 就会留下。
            svc.command = lambda: [
                sys.executable, "-c",
                "import sys, time;"
                "sys.stderr.write('板块[x] 开始抓取\\n' * 500);"
                "sys.stderr.flush();"
                "time.sleep(30);"
                "open(sys.argv[1], 'w').write('x')",
                str(marker),
            ]
            with patch.object(service, "LATEST_FILE", root / "latest_run.json"), \
                 patch.object(service, "STDERR_LOG_FILE", log_file):
                started = time.time()
                ok, message = svc.run_once("test")
                elapsed = time.time() - started
            kept = log_file.read_text(encoding="utf-8")
            snapshot = svc.snapshot()

        self.assertFalse(ok)
        self.assertEqual("scrape timed out after 1s", message)
        self.assertEqual("scrape timed out after 1s", snapshot["last_error"])
        self.assertEqual("error", snapshot["status"])
        self.assertEqual(1, snapshot["runs"])
        self.assertIsNotNone(snapshot["last_finished_at"])
        self.assertLess(elapsed, 15)          # 到点就收口，不再「跨过 N×容差还活着」
        self.assertFalse(marker.exists())     # 子进程确实被 kill 了
        # 超时这条路上，子进程死前的 stderr 也整段留下了（头部不截）
        self.assertGreater(len(kept), service.MAX_STDERR_KEPT)
        self.assertIn("板块[x] 开始抓取", kept)
        self.assertEqual(len(kept), snapshot["last_stderr_bytes"])

    def test_full_stderr_is_kept_beyond_the_status_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_file = root / "scrape_stderr.log"
            long_stderr = "".join(f"[09:00:{i % 60:02d}] 板块[{i}] 开始抓取\n" for i in range(400))
            svc = self.build(SCRAPE_MODE="rss")
            result = SimpleNamespace(returncode=0, stdout=json.dumps([{"Topic ID": "1"}]),
                                     stderr=long_stderr)
            with patch.object(service, "LATEST_FILE", root / "latest_run.json"), \
                 patch.object(service, "STDERR_LOG_FILE", log_file), \
                 patch.object(service, "run_with_wall_clock_deadline", return_value=result):
                ok, _ = svc.run_once("test")
            kept = log_file.read_text(encoding="utf-8")
            snapshot = svc.snapshot()

        self.assertTrue(ok)
        self.assertEqual(long_stderr, kept)                     # 一字不少
        self.assertIn("板块[0] 开始抓取", kept)                  # 最早那几行正是被切掉的那些
        self.assertGreater(len(kept), service.MAX_STDERR_KEPT)   # 确实超过 /status 的尾部上限
        self.assertLessEqual(len(snapshot["last_stderr"]), service.MAX_STDERR_KEPT)
        self.assertEqual(len(kept), snapshot["last_stderr_bytes"])
        self.assertTrue(snapshot["stderr_log_file"].endswith("scrape_stderr.log"))


class RoundScheduleTests(unittest.TestCase):
    """POLL-78 验收 2：next_run_at 按**轮次起点**顺延，不被收口时刻推后。"""

    def build(self, **env):
        with patch.dict(os.environ, env):
            return service.ScrapeService()

    def test_next_run_at_is_anchored_on_the_round_start(self):
        t0 = datetime(2026, 9, 30, 0, 0, 0, tzinfo=timezone.utc)
        round_duration = timedelta(hours=5, minutes=32, seconds=54)   # 合盖那轮的墙钟耗时
        svc = self.build(SCRAPE_MODE="rss", SCRAPE_INTERVAL_SECONDS="21600")   # 6h
        clock = {"now": t0}
        announced = []

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock["now"]

        def fake_wait_until(due, tick=30):
            if len(announced) >= 2:          # 两轮够了，退出调度循环
                return False
            clock["now"] = due               # 墙钟走到点
            return True

        def fake_run_once(trigger):
            announced.append(svc.snapshot()["next_run_at"])
            clock["now"] += round_duration
            return True, "ok"

        with patch.object(svc, "should_run_on_start", return_value=True), \
             patch.object(svc, "run_once", side_effect=fake_run_once), \
             patch.object(svc, "wait_until", side_effect=fake_wait_until), \
             patch.object(service, "datetime", FakeDatetime):
            svc.scheduler()

        def at(hours):
            return (t0 + timedelta(hours=hours)).isoformat(timespec="seconds")

        # 第 1 轮起点 t0 → 下一轮 t0+6h；第 2 轮起点 t0+6h → 再下一轮 t0+12h。
        # 旧口径第 2 轮报的是 t0+11h32m54s（收口时刻 + 6h）—— 节奏被轮次耗时推着走。
        self.assertEqual([at(6), at(12)], announced)


class CfChallengeWallClockTests(unittest.TestCase):
    """POLL-78：CF 挑战等待（同源处）核实 —— 墙钟口径，且 deadline 覆盖 goto 本身。"""

    @staticmethod
    def browser_utils_without_playwright():
        """browser_utils 顶层就 import playwright（CI 没装），先塞个假的进 sys.modules。"""
        fake = SimpleNamespace(sync_playwright=lambda: None)
        with patch.dict("sys.modules", {
            "playwright": SimpleNamespace(sync_api=fake),
            "playwright.sync_api": fake,
        }):
            import browser_utils
        return browser_utils

    def test_goto_overrun_is_counted_against_the_same_deadline(self):
        browser_utils = self.browser_utils_without_playwright()
        clock = {"now": 1000.0}

        def fake_time():
            clock["now"] += 30           # 每次读表都往前 30 秒
            return clock["now"]

        def goto(url, timeout=None):
            clock["now"] += 3600         # 合盖：goto 内部按单调钟计时，60s 跨成 1 小时墙钟

        page = SimpleNamespace(goto=goto, title=lambda: "Just a moment...")
        started = time.time()
        with patch.object(browser_utils.time, "time", fake_time):
            ok = browser_utils.wait_cf_challenge(page, "https://linux.do/t/1", timeout=60)
        elapsed = time.time() - started

        self.assertFalse(ok)             # goto 已经超了，不再续一轮 60 秒的循环
        self.assertLess(elapsed, 1.0)


class ReadEndpointAuthTests(unittest.TestCase):
    """P5：开启读接口鉴权后，无令牌 401、带令牌 200，/run 门禁不受影响。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [
            patch.object(service, "LATEST_FILE", root / "latest_run.json"),
            patch.object(service, "HOT_FILE", root / "hot_topics.json"),
        ]
        for item in self.patches:
            item.start()
        with patch.dict(os.environ, {"SCRAPE_MODE": "rss", "SERVICE_TOKEN": "s3cret",
                                     "PROTECT_READ_ENDPOINTS": "true"}):
            self.svc = service.ScrapeService()
        self.svc.run_once = unittest.mock.Mock(return_value=(True, "ok"))
        with self.svc.state_lock:
            # /ready 只在服务离开 starting 后返回 200；这里直接给一个已就绪的状态。
            self.svc.state["status"] = "idle"
        self.patches.append(patch.object(service, "SERVICE", self.svc))
        self.patches.append(patch.object(service.Handler, "log_message", lambda *args: None))
        self.patches[-1].start()
        self.patches[-2].start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def call(self, path, token=None, method="GET"):
        request = urllib.request.Request(self.base + path, method=method)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_read_endpoints_reject_missing_token(self):
        for path in ("/status", "/topics", "/hot"):
            with self.subTest(path=path):
                self.assertEqual(401, self.call(path)[0])

    def test_read_endpoints_reject_wrong_token(self):
        self.assertEqual(401, self.call("/status", token="wrong")[0])

    def test_read_endpoints_accept_the_configured_token(self):
        for path in ("/status", "/topics", "/hot"):
            with self.subTest(path=path):
                status, _ = self.call(path, token="s3cret")
                self.assertEqual(200, status)

    def test_probes_stay_open_without_a_token(self):
        self.assertEqual(200, self.call("/health")[0])
        self.assertEqual(200, self.call("/ready")[0])

    def test_manual_run_gate_is_unchanged(self):
        self.assertEqual(401, self.call("/run", method="POST")[0])
        self.assertEqual(401, self.call("/run", token="wrong", method="POST")[0])
        status, body = self.call("/run", token="s3cret", method="POST")
        self.assertEqual(202, status)
        self.assertTrue(body["accepted"])


class DeploymentDefaultsTests(unittest.TestCase):
    """P5：默认部署姿态 —— 本机绑回环，容器里读接口默认带鉴权。"""

    def test_default_bind_is_loopback(self):
        environ = {k: v for k, v in os.environ.items() if k != "SERVICE_HOST"}
        with patch.dict(os.environ, environ, clear=True):
            self.assertEqual("127.0.0.1", service.resolve_host())

    def test_explicit_host_wins(self):
        with patch.dict(os.environ, {"SERVICE_HOST": "0.0.0.0"}):
            self.assertEqual("0.0.0.0", service.resolve_host())

    def test_compose_protects_reads_and_publishes_loopback_only(self):
        compose = (REPO_ROOT / "docker-compose.service.yml").read_text(encoding="utf-8")
        # 容器内必须对外监听才收得到端口映射，因此读接口默认鉴权。
        self.assertIn('SERVICE_HOST: "0.0.0.0"', compose)
        self.assertIn('PROTECT_READ_ENDPOINTS: "${PROTECT_READ_ENDPOINTS:-true}"', compose)
        # 端口只发布到宿主机回环，不对整个局域网开放。
        self.assertIn('"127.0.0.1:8080:8080"', compose)

    def test_readme_documents_the_shipped_defaults(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertRegex(readme, r"\|\s*`SERVICE_HOST`\s*\|\s*`127\.0\.0\.1`\s*\|")
        self.assertRegex(readme, r"\|\s*`PROTECT_READ_ENDPOINTS`\s*\|\s*`false`\s*\|")


class ServiceImageTests(unittest.TestCase):
    """P2/P3：镜像必须装得下 service.py 真正用到的那套模块与依赖。

    本机没有 docker（构建条件不具备），所以用**静态闭包**代替构建验证：
    从 service.py 出发，把「shell out 到的脚本」和「这些脚本 import 到的本地模块」
    全部走一遍，逐个断言 Dockerfile.service 里有 COPY。P2（缺 daily_report.py）
    与 P3（缺 browser_utils.py / hot_topics.py）都会被这条断言抓住。
    真实构建与容器内跑一轮 browser 模式仍然未验证，见交付说明。
    """

    def local_imports(self, path):
        """文件里所有顶层 import 的模块名（含函数内的延迟 import）。"""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        return names

    def required_modules(self):
        service_source = (REPO_ROOT / "service.py").read_text(encoding="utf-8")
        needed = {"service.py"}
        # service.py 用 ROOT / "xxx.py" 的形式起子进程，这些文件不出现在 import 里
        needed.update(re.findall(r'ROOT\s*/\s*"([\w.]+\.py)"', service_source))
        frontier = list(needed)
        while frontier:
            for name in self.local_imports(REPO_ROOT / frontier.pop()):
                candidate = f"{name}.py"
                if (REPO_ROOT / candidate).exists() and candidate not in needed:
                    needed.add(candidate)
                    frontier.append(candidate)
        return needed

    def copied_modules(self, dockerfile):
        # 先把反斜杠续行接回一行，否则续行上的文件名会被整个漏掉
        copied = set()
        for line in dockerfile.replace("\\\n", " ").splitlines():
            stripped = line.strip()
            if stripped.startswith("COPY"):
                copied.update(token for token in stripped.split() if token.endswith(".py"))
        return copied

    def test_image_copies_every_module_reachable_from_the_service(self):
        dockerfile = (REPO_ROOT / "Dockerfile.service").read_text(encoding="utf-8")
        missing = self.required_modules() - self.copied_modules(dockerfile)
        self.assertEqual(set(), missing, f"镜像缺这些运行期模块: {sorted(missing)}")
        # 反过来也确认闭包不是空的、确实覆盖了 P2/P3 里点名的文件
        self.assertTrue({"daily_report.py", "browser_utils.py", "hot_topics.py"}
                        <= self.required_modules())

    def test_browser_stage_installs_what_browser_mode_needs(self):
        dockerfile = (REPO_ROOT / "Dockerfile.service").read_text(encoding="utf-8")
        browser_requirements = (REPO_ROOT / "requirements-browser.txt").read_text(encoding="utf-8")
        browser_utils = (REPO_ROOT / "browser_utils.py").read_text(encoding="utf-8")

        self.assertIn("FROM base AS browser", dockerfile)
        self.assertIn("FROM base AS rss", dockerfile)          # 默认目标 = 最后一个 stage
        self.assertIn("playwright", browser_requirements)
        self.assertIn("-r requirements-service.txt", browser_requirements)
        # browser 模式用系统 Chrome（channel="chrome"），所以镜像必须装 Chrome，
        # 且因为不能用 headless（会被 CF 403），还需要 Xvfb 提供虚拟显示。
        self.assertIn('channel="chrome"', browser_utils)
        self.assertIn("google-chrome-stable", dockerfile)
        self.assertIn("xvfb", dockerfile)


class CiCoverageTests(unittest.TestCase):
    """P6：CI 必须语法检查全仓每个模块，尤其是此前零覆盖的那几个。"""

    PREVIOUSLY_UNCOVERED = [
        "linux_do_gui.py",
        "linux_do_headless.py",
        "linux_do_auto_browse.py",
        "hot_topics.py",
        "browser_utils.py",
        "docker/linux_do_docker.py",
    ]

    def test_ci_compiles_every_tracked_module(self):
        workflow = (REPO_ROOT / ".github/workflows/test.yml").read_text(encoding="utf-8")
        # 覆盖面必须来自仓库（git ls-files），写死清单会再次漏掉新文件。
        self.assertIn("git ls-files '*.py'", workflow)

        if not (REPO_ROOT / ".git").exists():
            self.skipTest("非 git 检出（tarball）环境，跳过清单核对")
        tracked = subprocess.run(["git", "-C", str(REPO_ROOT), "ls-files", "*.py"],
                                 capture_output=True, text=True, check=True).stdout.split()
        for name in self.PREVIOUSLY_UNCOVERED:
            self.assertIn(name, tracked)
        self.assertGreaterEqual(len(tracked), 13)

        # 本地跑一遍 CI 的等价命令：每个受版本控制的模块都要能编译。
        for name in tracked:
            with self.subTest(module=name):
                self.assertTrue(py_compile.compile(str(REPO_ROOT / name), doraise=True))


class DateTimeGateTests(unittest.TestCase):
    """2026-10-01 事故回归：DOM 回退把标签文本写进时间字段 → 飞书 800010403，
    且 record-batch-create 整批原子 → 937 条一条没进去。"""

    # 事故里的原值：'link-bottom-line' 在列表页装的是标签，被当成创建时间。
    TAG_TEXT = "纯水,人工智能,ChatGPT,OpenAI"
    CN_LABEL = "创建日期：2026 年 9月 29 日"

    def test_gate_rejects_non_time_text(self):
        for junk in (self.TAG_TEXT, "软件开发", "快问快答", "此话题已对您置顶", ""):
            with self.subTest(junk=junk):
                self.assertEqual(feishu.normalize_datetime(junk), "")

    def test_gate_accepts_real_forms(self):
        cases = {
            "2026-09-29 12:34:56": "2026-09-29 12:34:56",
            "2026-09-29T12:34:56.000+08:00": "2026-09-29 12:34:56",
            "2026-09-29": "2026-09-29 00:00:00",
            self.CN_LABEL: "2026-09-29 00:00:00",
            "1790833592414": "2026-10-01 05:46:32",  # 毫秒戳（Discourse data-time）
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(feishu.normalize_datetime(raw), want)

    def test_both_modules_share_the_same_gate(self):
        """抓取侧和推送侧口径必须一致，否则只改一处会留下反例。"""
        samples = [self.TAG_TEXT, self.CN_LABEL, "2026-09-29T12:34:56Z", "", None,
                   "1790833592414", "快问快答"]
        for raw in samples:
            with self.subTest(raw=raw):
                self.assertEqual(scraper.normalize_datetime(raw), feishu.normalize_datetime(raw))

    def test_sanitize_fixes_already_formatted_rows(self):
        """pending_feishu.json 这类已格式化行不走 feishu_rows()，闸门必须覆盖到。"""
        rows = [
            {"标题": "a", "Topic ID": "1", "发布时间": self.TAG_TEXT, "最近活跃": self.CN_LABEL},
            {"标题": "b", "Topic ID": "2", "发布时间": "2026-09-29 12:34:56", "最近活跃": "2026-09-29 12:34:56"},
        ]
        fixed, patched = feishu.sanitize_rows(rows)
        self.assertEqual(patched, 2)                       # 脏字符串 + 中文日期标签各算一次
        self.assertEqual(fixed[0]["发布时间"], "")          # 非时间文本 → 留空，不再写脏值
        self.assertEqual(fixed[0]["最近活跃"], "2026-09-29 00:00:00")
        self.assertEqual(fixed[1]["发布时间"], "2026-09-29 12:34:56")  # 合法值原样保留
        # 原行不被就地改写
        self.assertEqual(rows[0]["发布时间"], self.TAG_TEXT)

    def test_empty_datetime_is_dropped_not_sent_as_blank(self):
        """飞书 datetime 字段收到空串同样 400，只有「键不存在」才等于留空。"""
        payload = feishu.payload_records([
            {"标题": "a", "Topic ID": "1", "发布时间": "", "最近活跃": "2026-09-29 12:34:56"},
        ])
        self.assertNotIn("发布时间", payload[0])
        self.assertIn("最近活跃", payload[0])

    def test_one_poison_row_does_not_kill_the_batch(self):
        """整批被拒时二分重试：坏行隔离，其余照写（旧行为是 break，全批丢光）。"""
        poison = "POISON"
        rows = [{"标题": f"t{i}", "Topic ID": str(i)} for i in range(5)]
        rows[2]["Topic ID"] = poison
        calls = []

        def fake_create(base_token, table, chunk, dry_run=False):
            calls.append(len(chunk))
            if any(r["Topic ID"] == poison for r in chunk):
                return 0, "800010403 invalid_request"
            return len(chunk), None

        with patch.object(feishu, "create_records", side_effect=fake_create):
            written, rejects = feishu.push("base", "table", rows)

        self.assertEqual(written, 4)
        self.assertEqual([r["Topic ID"] for r in rejects], [poison])
        self.assertGreater(len(calls), 1)   # 确实拆过批

    def test_dom_path_no_longer_falls_back_to_raw_text(self):
        """抓取侧不许再把整段文本当时间回退（.link-bottom-line 里装的是标签）。"""
        source = (REPO_ROOT / "linux_do_scraper.py").read_text(encoding="utf-8")
        self.assertIn("data-time", source)                  # 毫秒时间戳来源
        self.assertIn("创建日期", source)                    # td.activity[title] 里的中文标签
        self.assertNotIn("b2.textContent.trim().substring(0, 60)", source)
        self.assertNotIn("actTd.textContent.trim()", source)


if __name__ == "__main__":
    unittest.main()
