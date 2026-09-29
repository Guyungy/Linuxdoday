import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import linux_do_scraper as scraper
import push_to_feishu as feishu
import daily_report as report
import service


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


if __name__ == "__main__":
    unittest.main()
