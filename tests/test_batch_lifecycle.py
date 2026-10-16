import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import fetch_content

# load_browser_utils 是 test_core 里的共用助手，不在这里复制一份。两种跑法把不同的
# 目录放进 sys.path，所以要两条路径都认：CI 的 `discover -s tests` 把 tests/ 放在
# sys.path[0]，直接跑 `python -m unittest tests.test_batch_lifecycle` 放的是仓库根。
try:
    from test_core import load_browser_utils
except ImportError:
    from tests.test_core import load_browser_utils


class BrowserLifecycleTests(unittest.TestCase):
    def test_profile_lease_excludes_other_jobs_and_releases_on_close(self):
        browser = load_browser_utils()
        with tempfile.TemporaryDirectory() as directory, patch.object(browser, 'USER_DATA_DIR', directory):
            lease = browser.acquire_profile_lease()
            try:
                with self.assertRaisesRegex(RuntimeError, '其他抓取任务'):
                    browser.acquire_profile_lease()
            finally:
                lease.close()
            browser.acquire_profile_lease().close()

    def test_close_failure_still_stops_driver_and_releases_lease(self):
        browser = load_browser_utils()
        context, driver, lease = Mock(), Mock(), Mock()
        context.close.side_effect = RuntimeError('browser closed')
        session = browser.BrowserSession(context, driver, lease)
        with self.assertRaises(RuntimeError):
            session.close()
        session.close()
        context.close.assert_called_once()
        driver.stop.assert_called_once()
        lease.close.assert_called_once()

    def test_failed_launch_releases_profile_and_driver(self):
        browser = load_browser_utils()
        driver = Mock()
        driver.chromium.launch_persistent_context.side_effect = RuntimeError('launch failed')
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(browser, 'USER_DATA_DIR', directory), \
                patch.object(browser, 'sync_playwright', return_value=SimpleNamespace(start=lambda: driver)):
            with self.assertRaisesRegex(RuntimeError, 'launch failed'):
                browser.start_browser()
            driver.stop.assert_called_once()
            browser.acquire_profile_lease().close()


class ContentBatchTests(unittest.TestCase):
    def test_reuse_retry_and_checkpoint_preserve_results(self):
        rows = [{'id': str(i)} for i in range(3)]
        contents = {'0': {'content': 'cached'}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(fetch_content, 'fetch_content', side_effect=[None, {'content': 'retried'}, None, None]) as fetch, \
                patch.object(fetch_content.time, 'sleep'), \
                patch.object(fetch_content, 'log'):
            path = str(Path(directory) / 'content.json')
            result = fetch_content.fetch_batch(None, rows, contents, path=path, checkpoint=1)
            self.assertEqual({'success': 2, 'failed': 1, 'reused': 1}, result)
            self.assertEqual(4, fetch.call_count)
            self.assertEqual('cached', rows[0]['content'])
            self.assertEqual('retried', json.loads(Path(path).read_text())['1']['content'])
            self.assertNotIn('2', contents)

    def test_browser_failure_checkpoints_completed_work(self):
        rows = [{'id': '1'}, {'id': '2'}]
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(fetch_content, 'fetch_content', side_effect=[{'content': 'saved'}, RuntimeError('closed')]), \
                patch.object(fetch_content.time, 'sleep'):
            path = str(Path(directory) / 'content.json')
            with self.assertRaisesRegex(RuntimeError, 'closed'):
                fetch_content.fetch_batch(None, rows, {}, path=path)
            self.assertEqual({'1': {'content': 'saved'}}, json.loads(Path(path).read_text()))

    def test_standalone_backfill_fetches_empty_cached_body(self):
        with tempfile.TemporaryDirectory() as directory:
            topics = Path(directory) / 'topics.json'
            cache = Path(directory) / 'content.json'
            topics.write_text(json.dumps([{'id': '1'}, {'id': '2'}]))
            cache.write_text(json.dumps({'1': {'content': ''}, '2': {'content': 'done'}}))
            context = Mock()
            with patch.object(fetch_content, 'CACHE_FILE', str(topics)), \
                    patch.object(fetch_content, 'CONTENT_FILE', str(cache)), \
                    patch.object(fetch_content.sys, 'argv', ['fetch_content.py']), \
                    patch.object(fetch_content, 'start_browser', return_value=(context, None)), \
                    patch.object(fetch_content, 'check_login', return_value=True), \
                    patch.object(fetch_content, 'fetch_content', return_value={'content': 'filled'}) as fetch, \
                    patch.object(fetch_content.time, 'sleep'), \
                    patch.object(fetch_content, 'log'):
                fetch_content.main()
            fetch.assert_called_once()
            self.assertEqual('1', fetch.call_args.args[1]['id'])
            self.assertEqual('filled', json.loads(cache.read_text())['1']['content'])
            self.assertEqual('done', json.loads(cache.read_text())['2']['content'])
            context.close.assert_called_once()

    def test_rss_batch_needs_no_browser_requests(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(fetch_content, 'fetch_content') as fetch, \
                patch.object(fetch_content.time, 'sleep') as sleep, \
                patch.object(fetch_content, 'log'):
            rows = [{'id': '1'}]
            result = fetch_content.fetch_batch(None, rows, {}, supplied={'1': 'rss'},
                                               path=str(Path(directory) / 'content.json'))
            self.assertEqual(1, result['success'])
            self.assertEqual('rss', rows[0]['content'])
            fetch.assert_not_called()
            sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
