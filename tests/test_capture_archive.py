from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from parsing_site.capture_archive import capture_due, period_path
from parsing_site.db import Database
from parsing_site.settings_store import SettingsStore, from_dict, validate_settings


class FakeSession:
    def __init__(self) -> None:
        self.images = 0
        self.pages = 0
        self.image_args: tuple[str, int, int] | None = None

    def capture_map_image(self, selector: str, width: int, height: int) -> bytes:
        self.images += 1
        self.image_args = (selector, width, height)
        return b"jpeg image"

    def capture_html(self) -> str:
        self.pages += 1
        return "<html>map</html>"


class CaptureArchiveTest(unittest.TestCase):
    def test_period_buckets_and_saved_preferences(self) -> None:
        with TemporaryDirectory() as directory:
            settings = validate_settings(from_dict({
                "screenshot_enabled": True, "screenshot_dir": str(Path(directory) / "shots"),
                "screenshot_period_hours": 3, "screenshot_width": 900,
                "screenshot_height": 600, "archive_html_enabled": True,
                "archive_html_dir": str(Path(directory) / "pages"),
                "archive_html_period_hours": 6,
            }))
            self.assertTrue(from_dict(settings.to_dict()).archive_html_enabled)
            self.assertEqual(from_dict(settings.to_dict()).screenshot_width, 900)
            db = Database(Path(directory) / "settings.db")
            store = SettingsStore(db)
            store.save(settings)
            self.assertTrue(store.load().screenshot_enabled)
            self.assertEqual(store.load().archive_html_period_hours, 6)
            db.close()
            session = FakeSession()
            first = datetime(2026, 10, 25, 7, 15, 30, tzinfo=UTC)
            saved, errors = capture_due(session, settings, 4, captured_at=first)
            self.assertFalse(errors)
            self.assertEqual(len(saved), 2)
            self.assertEqual(session.image_args, (settings.map_container_selector, 900, 600))
            self.assertEqual(
                period_path(settings.screenshot_dir, 3, first).name, "06-09"
            )
            self.assertTrue(all("2026-10-25_07-15-30_zoom-4" in path.name for path in saved))
            self.assertEqual(saved[1].read_text(encoding="utf-8"), "<html>map</html>")
            repeated, errors = capture_due(
                session, settings, 4,
                captured_at=datetime(2026, 10, 25, 8, 30, tzinfo=UTC),
            )
            self.assertEqual((repeated, errors), ([], []))
            self.assertEqual((session.images, session.pages), (1, 1))
            later, errors = capture_due(
                session, settings, 5,
                captured_at=datetime(2026, 10, 25, 9, 1, tzinfo=UTC),
            )
            self.assertFalse(errors)
            self.assertEqual(len(later), 1)  # Новый 3-часовой период снимков.
            self.assertEqual((session.images, session.pages), (2, 1))

    def test_archive_settings_reject_unsupported_period_and_size(self) -> None:
        with self.assertRaises(ValueError):
            validate_settings(from_dict({"screenshot_period_hours": 2}))
        with self.assertRaises(ValueError):
            validate_settings(from_dict({"screenshot_width": 100}))


if __name__ == "__main__":
    unittest.main()
