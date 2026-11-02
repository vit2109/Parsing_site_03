from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from parsing_site import config, launch_chrome
from parsing_site.browser import BrowserSession
from parsing_site.config import BrowserConfig
from parsing_site.db import Database
from parsing_site.settings_store import SettingsStore
from parsing_site.table_history_db import TableHistoryDatabase


class ParallelInstancesTest(unittest.TestCase):
    def test_browser_stays_on_its_selected_tab(self) -> None:
        first = SimpleNamespace(url="https://example.test/map", label="first")
        second = SimpleNamespace(url="https://example.test/map", label="second")
        context = SimpleNamespace(pages=[first, second])
        session = BrowserSession(BrowserConfig(url_contains="example.test/map"))
        session._browser = SimpleNamespace(contexts=[context])
        self.assertIs(session._find_page(), first)
        context.pages.reverse()
        self.assertIs(session._find_page(), first)
        context.pages.remove(first)
        self.assertIs(session._find_page(), second)

    def test_names_isolate_databases_settings_and_output(self) -> None:
        with TemporaryDirectory() as directory, patch.object(config, "ROOT", Path(directory)):
            paths = []
            for name, port, mode in (("algorithm_a", 9222, "adaptive"),
                                     ("algorithm_b", 9223, "followed_grid")):
                url = f"http://127.0.0.1:{port}"
                with patch.dict(os.environ, {
                    "PARSING_SITE_INSTANCE": name,
                    "PARSING_SITE_CDP_URL": url,
                }):
                    db = Database()
                    table_db = TableHistoryDatabase()
                    settings = SettingsStore(db, bootstrap_path=Path(directory) / name / "settings.json")
                    current = settings.ensure_defaults(config.load_config())
                    self.assertEqual(current.cdp_url, url)
                    current.scan_mode = mode
                    settings.save(current)
                    self.assertEqual(settings.load().scan_mode, mode)
                    self.assertEqual(config.instance_output_path("data/screenshots"),
                                     config.instance_data_dir() / "screenshots")
                    paths.append((db.path, table_db.path))
                    table_db.close()
                    db.close()
            self.assertNotEqual(paths[0], paths[1])
            for db_path, table_path in paths:
                self.assertTrue(db_path.is_file())
                self.assertTrue(table_path.is_file())
            first = Database(paths[0][0])
            second = Database(paths[1][0])
            self.assertEqual(SettingsStore(first).load().scan_mode, "adaptive")
            self.assertEqual(SettingsStore(second).load().scan_mode, "followed_grid")
            first.close()
            second.close()

    def test_chrome_ports_get_distinct_profiles(self) -> None:
        with TemporaryDirectory() as directory, \
                patch.object(launch_chrome, "ROOT", Path(directory)), \
                patch.object(launch_chrome, "find_chrome", return_value=Path("chrome.exe")), \
                patch.object(launch_chrome, "cdp_endpoint_ok", return_value=(False, "")), \
                patch.object(launch_chrome.subprocess, "Popen") as popen, \
                patch.object(launch_chrome.time, "sleep"):
            first = launch_chrome.launch_chrome(port=9222)
            second = launch_chrome.launch_chrome(port=9223)
            self.assertNotEqual(first, second)
            self.assertEqual(first.name, ".chrome-profile")
            self.assertEqual(second.name, ".chrome-profile-9223")
            self.assertIn(f"--user-data-dir={second}", popen.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
