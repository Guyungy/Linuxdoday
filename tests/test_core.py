import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import linux_do_scraper as scraper
import push_to_feishu as feishu
import service


class ScraperTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
