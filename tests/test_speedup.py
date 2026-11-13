from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import launcher
from parsing_site.browser import BrowserSession, NumberedCapture
from parsing_site.capture_archive import CaptureArchive
from parsing_site.config import BrowserConfig, TrackingConfig
from parsing_site.db import Database
from parsing_site.settings_store import SettingsStore, from_dict, validate_settings
from parsing_site.table_history_db import TableHistoryDatabase
from parsing_site.tracking import (
    _apply_number_cache,
    build_tracking_snapshot,
)
from parsing_site.worker import ParseWorker


class SpeedupTest(unittest.TestCase):
    def test_effective_preferences_are_reversible_and_selective(self) -> None:
        original = from_dict({
            "max_enabled": True, "screenshot_enabled": True,
            "archive_html_enabled": True, "scan_debug_html_enabled": True,
            "speedup_enabled": True, "speedup_disable_screenshots": False,
        })
        effective = original.effective()
        self.assertFalse(effective.max_enabled)
        self.assertTrue(effective.screenshot_enabled)
        self.assertFalse(effective.archive_html_enabled)
        self.assertFalse(effective.scan_debug_html_enabled)
        restored = replace(original, speedup_enabled=False).effective()
        for name in ("max_enabled", "screenshot_enabled", "archive_html_enabled", "scan_debug_html_enabled"):
            self.assertTrue(getattr(restored, name))
            self.assertTrue(getattr(original, name))

    def test_disabled_function_is_not_enabled_by_restore(self) -> None:
        original = from_dict({"max_enabled": False, "speedup_enabled": True})
        self.assertFalse(replace(original, speedup_enabled=False).effective().max_enabled)

    def test_preferences_survive_database_reload(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            try:
                store = SettingsStore(db)
                store.save(from_dict({"speedup_enabled": True, "history_keep_days": 45,
                                      "speedup_disable_html": False}))
                saved = SettingsStore(db).current()
                self.assertTrue(saved.speedup_enabled)
                self.assertFalse(saved.speedup_disable_html)
                self.assertEqual(saved.history_keep_days, 45)
            finally:
                db.close()

    def test_retention_cannot_be_less_than_twenty_days(self) -> None:
        with self.assertRaisesRegex(ValueError, "20"):
            validate_settings(from_dict({"history_keep_days": 19}))

    def test_archive_respects_speedup_changed_during_capture(self) -> None:
        with TemporaryDirectory() as directory:
            settings = from_dict({"screenshot_enabled": True, "archive_html_enabled": True,
                                  "screenshot_dir": directory, "archive_html_dir": directory})
            archive = CaptureArchive(settings, lambda: settings)
            capture = NumberedCapture(datetime.now().astimezone(), 7, b"jpeg", "<html/>")
            settings.speedup_enabled = True
            archive.save(capture)
            self.assertEqual(archive.saved, [])
            settings.speedup_enabled = False
            archive.save(capture)
            self.assertEqual(len(archive.saved), 2)

    def test_capture_skips_browser_work_when_speedup_was_enabled_mid_scan(self) -> None:
        settings = from_dict({"speedup_enabled": True, "screenshot_enabled": True,
                              "archive_html_enabled": True})
        session = BrowserSession(BrowserConfig())
        session.prepare_numbered_capture((1280, 720), html_enabled=True)
        session.prepare_capture_preferences(lambda: settings)
        page = MagicMock()
        self.assertFalse(session._capture_visible_view(page, ".leaflet-container", 7))
        page.evaluate.assert_not_called()

    def test_number_cache_survives_long_scan_but_rejects_distant_or_expired_id(self) -> None:
        for age, lat, expected in ((180, 50, 101), (180, 60, -1), (1201, 50, -1)):
            raw = {"dynamics": [{"index": -1, "fallback_id": -5, "lat": lat, "lng": 40}]}
            with patch("parsing_site.tracking.monotonic", return_value=2000):
                _apply_number_cache(raw, {-5: (101, 50, 40, 2000 - age)})
            self.assertEqual(raw["dynamics"][0]["index"], expected)

    def test_worker_publishes_rows_before_final_capture_and_saves_only_final(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            history = TableHistoryDatabase(Path(directory) / "history.db")
            try:
                store = SettingsStore(db)
                store.save(from_dict({"static_objects": [{"name": "Точка", "lat": 50, "lng": 40}]}))
                worker = ParseWorker(db, store, history)
                session = MagicMock()
                session.__enter__.return_value = session
                callbacks = []
                session.prepare_progress.side_effect = callbacks.append
                raw = {"dynamics": [{"index": 101, "name": "Объект", "x": 0, "y": 0,
                                     "lat": 50, "lng": 40.1}], "coordinate_mode": "latlng"}

                def capture(_session: object, tracking: TrackingConfig):
                    callbacks[0](raw)
                    status = worker.status
                    self.assertTrue(status["snapshot"]["in_progress"])
                    self.assertEqual(len(status["snapshot"]["rows"]), 1)
                    self.assertEqual(status["polls"], 0)
                    worker._stop.set()
                    return build_tracking_snapshot(url="test", title="test", raw=raw, tracking=tracking)

                with patch("parsing_site.worker.BrowserSession", return_value=session), \
                        patch("parsing_site.worker.capture_snapshot", side_effect=capture), \
                        patch.object(db, "save_snapshot", wraps=db.save_snapshot) as save, \
                        patch.object(history, "save_snapshot", wraps=history.save_snapshot) as save_table:
                    worker._run()
                self.assertEqual(worker.status["polls"], 1)
                self.assertNotIn("in_progress", worker.status["snapshot"])
                self.assertEqual(worker.status["revision"], 2)
                save.assert_called_once()
                save_table.assert_called_once()
            finally:
                db.close()
                history.close()

    def test_state_can_skip_expensive_statistics_and_unchanged_snapshot(self) -> None:
        from parsing_site import webapp

        fake_worker = MagicMock()
        fake_worker.status = {"running": True, "status": "running", "error": "", "polls": 1,
                              "revision": 8, "snapshot": {"rows": [{}]}}
        fake_store = MagicMock()
        fake_store.current.return_value = from_dict({})
        with patch.object(webapp, "worker", fake_worker), patch.object(webapp, "store", fake_store), \
                patch.object(webapp, "db") as db, patch.object(webapp, "table_history_db") as history:
            db.list_logs.return_value = []
            result = webapp.api_state(include_storage=False, since_revision=8)
            self.assertFalse(result["snapshot_changed"])
            self.assertIsNone(result["snapshot"])
            history.storage_stats.assert_not_called()
            self.assertTrue(webapp.api_state(include_storage=False, since_revision=7)["snapshot_changed"])

class LauncherAdoptionTest(unittest.TestCase):
    def test_discovery_requires_local_registry_and_authenticated_status(self) -> None:
        target = launcher.Target("Основная программа", 8082, 9222)
        with TemporaryDirectory() as directory:
            backend = Path(directory) / "main.py"
            data = backend.parent / "data"
            data.mkdir()
            registry = data / "launcher-8082.json"
            registry.write_text(json.dumps({"application": "parsing-site", "web_port": 8082,
                                           "token": "secret", "cdp_url": "http://127.0.0.1:9223",
                                           "instance": "algorithm_b"}), encoding="utf-8")
            with patch.object(launcher, "launcher_request", return_value=True):
                managed = launcher.discover_target(backend, target)
                self.assertEqual(managed.target.cdp_port, 9223)
                self.assertEqual(managed.target.instance, "algorithm_b")
                self.assertIsNone(managed.process)
            with patch.object(launcher, "launcher_request", return_value=False):
                self.assertIsNone(launcher.discover_target(backend, target))

    def test_start_adopts_running_app_and_starts_only_missing_instance(self) -> None:
        with TemporaryDirectory() as directory:
            backend = Path(directory) / "main.py"
            backend.touch()
            first, second = launcher.build_targets(True, ("8082", "9222", "8083", "9223"))
            adopted = launcher.ManagedTarget(first, "external")
            process = MagicMock()
            process.poll.return_value = None
            with patch.object(launcher, "port_occupied", side_effect=[True, False]), \
                    patch.object(launcher, "discover_target", return_value=adopted), \
                    patch.object(launcher, "web_ready", return_value=True), \
                    patch.object(launcher.subprocess, "Popen", return_value=process) as popen:
                results = launcher.start_targets(backend, [first, second], False, lambda _text: None)
            self.assertIs(results[0], adopted)
            self.assertEqual(results[1].target, second)
            popen.assert_called_once()
            self.assertIn("8083", popen.call_args.args[0])

    def test_unrelated_occupied_port_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            backend = Path(directory) / "main.py"
            backend.touch()
            with patch.object(launcher, "port_occupied", return_value=True), \
                    patch.object(launcher, "discover_target", return_value=None), \
                    patch.object(launcher.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(RuntimeError, "8082"):
                    launcher.start_targets(backend, [launcher.Target("test", 8082, 9222)], False, print)
                popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
