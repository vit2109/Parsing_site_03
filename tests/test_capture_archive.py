from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from parsing_site.browser import BrowserSession
from parsing_site.capture_archive import capture_due, period_path, screenshot_due
from parsing_site.config import BrowserConfig, TrackingConfig
from parsing_site.db import Database
from parsing_site.settings_store import SettingsStore, from_dict, validate_settings


class FakeSession:
    def __init__(self) -> None:
        self.images = 0
        self.pages = 0
        self.image_args: tuple[str, int, int] | None = None
        self.numbered_image: tuple[bytes, float] | None = None
        self.numbered_capture_error = ""

    def capture_map_image(self, selector: str, width: int, height: int) -> bytes:
        self.images += 1
        self.image_args = (selector, width, height)
        return b"jpeg image"

    def capture_html(self) -> str:
        self.pages += 1
        return "<html>map</html>"

    def take_numbered_image(self) -> tuple[bytes, float] | None:
        image = self.numbered_image
        self.numbered_image = None
        return image


class CaptureArchiveTest(unittest.TestCase):
    def test_mouse_image_requires_visible_dom_label(self) -> None:
        class Page:
            visible = 0

            def evaluate(self, _script: str, _selector: str) -> int:
                return self.visible

        session = BrowserSession(BrowserConfig())
        session.prepare_numbered_capture((900, 600))
        page = Page()
        with patch.object(session, "capture_map_image", return_value=b"jpeg") as image:
            self.assertFalse(session._capture_mouse_numbered(
                page, {"map_zoom": 7}, ".leaflet-container"
            ))
            image.assert_not_called()
            page.visible = 2
            self.assertTrue(session._capture_mouse_numbered(
                page, {"map_zoom": 7}, ".leaflet-container"
            ))
        self.assertEqual(session.take_numbered_image(), (b"jpeg", 7))

    def test_leaflet_image_waits_for_labels_and_restores_view(self) -> None:
        class Page:
            def __init__(self, labels_after: int) -> None:
                self.zoom = 4
                self.labels_after = labels_after
                self.checks = 0
                self.waits: list[int] = []

            def evaluate(self, script: str, argument: object) -> object:
                if "const original =" in script:
                    self.zoom = argument["zoom"]
                    return {"lat": 50, "lng": 30, "zoom": 4}
                if "querySelectorAll" in script:
                    self.checks += 1
                    return int(self.checks >= self.labels_after)
                self.zoom = argument["zoom"]
                return None

            def wait_for_timeout(self, milliseconds: int) -> None:
                self.waits.append(milliseconds)

        session = BrowserSession(BrowserConfig())
        session.prepare_numbered_capture((900, 600))
        page = Page(labels_after=2)

        def screenshot(_selector: str, _width: int, _height: int) -> bytes:
            self.assertEqual(page.zoom, 7)
            self.assertEqual(page.checks, 2)
            return b"jpeg"

        with patch.object(session, "capture_map_image", side_effect=screenshot) as image:
            session._capture_best_numbered_view(
                page, TrackingConfig(), {"lat": 51, "lng": 31, "zoom": 7, "count": 3}
            )
        self.assertEqual(image.call_count, 1)
        self.assertEqual(session.take_numbered_image(), (b"jpeg", 7))
        self.assertEqual(page.zoom, 4)
        self.assertEqual(page.waits, [1500, 1500, 1500])

        session.prepare_numbered_capture((900, 600))
        page = Page(labels_after=3)
        with patch.object(session, "capture_map_image") as image:
            session._capture_best_numbered_view(
                page, TrackingConfig(), {"lat": 51, "lng": 31, "zoom": 7, "count": 3}
            )
        image.assert_not_called()
        self.assertIsNone(session.take_numbered_image())
        self.assertEqual(page.zoom, 4)

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
            self.assertTrue(screenshot_due(settings, first))
            session.numbered_image = (session.capture_map_image(
                settings.map_container_selector, 900, 600
            ), 7)
            saved, errors = capture_due(session, settings, 4, captured_at=first)
            self.assertFalse(errors)
            self.assertEqual(len(saved), 2)
            self.assertEqual(session.image_args, (settings.map_container_selector, 900, 600))
            self.assertEqual(
                period_path(settings.screenshot_dir, 3, first).name, "06-09"
            )
            self.assertIn("zoom-7", saved[0].name)
            self.assertIn("zoom-4", saved[1].name)
            self.assertFalse(screenshot_due(settings, first))
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
            self.assertEqual(later, [])  # Без видимого номера пустой снимок не пишем.
            self.assertTrue(screenshot_due(settings, datetime(2026, 10, 25, 9, 1, tzinfo=UTC)))
            session.numbered_image = (b"numbered view", 8)
            later, errors = capture_due(
                session, settings, 5,
                captured_at=datetime(2026, 10, 25, 9, 2, tzinfo=UTC),
            )
            self.assertFalse(errors)
            self.assertEqual(len(later), 1)
            self.assertIn("zoom-8", later[0].name)
            self.assertEqual((session.images, session.pages), (1, 1))

    def test_archive_settings_reject_unsupported_period_and_size(self) -> None:
        with self.assertRaises(ValueError):
            validate_settings(from_dict({"screenshot_period_hours": 2}))
        with self.assertRaises(ValueError):
            validate_settings(from_dict({"screenshot_width": 100}))


if __name__ == "__main__":
    unittest.main()
