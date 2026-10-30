from __future__ import annotations

import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from parsing_site.browser import BrowserSession, NumberedCapture
from parsing_site.capture_archive import CaptureArchive, capture_due, period_path
from parsing_site.config import BrowserConfig, TrackingConfig
from parsing_site.db import Database
from parsing_site.settings_store import SettingsStore, from_dict, validate_settings


class FakeSession:
    def __init__(self) -> None:
        self.numbered_capture: NumberedCapture | None = None
        self.numbered_capture_error = ""
        self.numbered_html_error = ""

    def take_numbered_capture(self) -> NumberedCapture | None:
        capture = self.numbered_capture
        self.numbered_capture = None
        return capture


class CaptureArchiveTest(unittest.TestCase):
    def test_tracked_tab_is_clicked_before_every_map_scan(self) -> None:
        events: list[str] = []

        class Tab:
            first: Tab

            def __init__(self) -> None:
                self.first = self

            def count(self) -> int:
                return 1

            def click(self, *, timeout: int) -> None:
                self_test.assertEqual(timeout, 10_000)
                events.append("click")

        class Page:
            url = "https://example.test/map"

            def title(self) -> str:
                return "Карта"

            def get_by_role(self, role: str, *, name: object) -> Tab:
                self_test.assertEqual(role, "tab")
                self_test.assertIsNotNone(name.search("5 сопровождается"))
                return Tab()

            def wait_for_timeout(self, milliseconds: int) -> None:
                self_test.assertEqual(milliseconds, 1500)
                events.append("wait")

        self_test = self
        session = BrowserSession(BrowserConfig())

        def scan(*_args: object, **_kwargs: object) -> dict:
            events.append("scan")
            return {"scan": {}}

        with patch.object(session, "_ready_page", return_value=Page()), \
                patch("parsing_site.browser.extract_scene_with_mouse_scan", side_effect=scan):
            session.capture_scene(TrackingConfig(scan_wait_seconds=1.5))
            session.capture_scene(TrackingConfig(scan_wait_seconds=1.5))
        self.assertEqual(events, ["click", "wait", "scan"] * 2)

    def test_each_numbered_approach_saves_a_separate_image_immediately(self) -> None:
        with TemporaryDirectory() as directory:
            settings = validate_settings(from_dict({
                "screenshot_enabled": True,
                "screenshot_dir": directory,
                "archive_html_enabled": True,
                "archive_html_dir": str(Path(directory) / "pages"),
            }))
            archive = CaptureArchive(settings)
            session = BrowserSession(BrowserConfig())
            session.prepare_numbered_capture(
                (900, 600), html_enabled=True, on_capture=archive.save
            )

            class Page:
                def __init__(self) -> None:
                    self.html_calls = 0

                def evaluate(self, _script: str, _selector: str) -> int:
                    return 2

                def content(self) -> str:
                    self.html_calls += 1
                    return "<html>numbered</html>"

            page = Page()
            with patch.object(session, "capture_map_image", side_effect=[
                b"jpg-7", b"jpg-7-again", b"jpg-8"
            ]) as image:
                self.assertTrue(session._capture_visible_view(
                    page, ".leaflet-container", 7
                ))
                self.assertEqual(len(archive.saved), 2)
                self.assertTrue(session._capture_visible_view(
                    page, ".leaflet-container", 7
                ))
                self.assertEqual(len(archive.saved), 3)
                self.assertTrue(session._capture_visible_view(
                    page, ".leaflet-container", 8
                ))
            self.assertEqual(image.call_count, 3)
            self.assertEqual(page.html_calls, 1)
            self.assertEqual(len(archive.saved), 4)
            self.assertEqual([path.read_bytes() for path in archive.saved if path.suffix == ".jpg"],
                             [b"jpg-7", b"jpg-7-again", b"jpg-8"])
            self.assertEqual(archive.finish(session)[1], [])

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
        capture = session.take_numbered_capture()
        self.assertEqual((capture.image, capture.zoom), (b"jpeg", 7))

    def test_leaflet_image_waits_for_labels_and_restores_view(self) -> None:
        class Page:
            def __init__(self, labels_after: int) -> None:
                self.zoom = 4
                self.labels_after = labels_after
                self.checks = 0
                self.waits: list[int] = []
                self.html_zoom: int | None = None

            def evaluate(self, script: str, argument: object = None) -> object:
                if "const original =" in script:
                    return {"lat": 50, "lng": 30, "zoom": 4}
                if "querySelectorAll" in script:
                    self.checks += 1
                    return int(self.checks >= self.labels_after)
                self.zoom = argument["zoom"]
                return None

            def wait_for_timeout(self, milliseconds: int) -> None:
                self.waits.append(milliseconds)

            def content(self) -> str:
                self.html_zoom = self.zoom
                return f"<html>zoom-{self.zoom}</html>"

        session = BrowserSession(BrowserConfig())
        session.prepare_numbered_capture((900, 600), html_enabled=True)
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
        capture = session.take_numbered_capture()
        self.assertEqual((capture.image, capture.zoom), (b"jpeg", 7))
        self.assertEqual(capture.html, "<html>zoom-7</html>")
        self.assertEqual(page.html_zoom, 7)
        self.assertEqual(page.zoom, 4)
        self.assertEqual(page.waits, [100, 1500, 1500, 100, 1500])

        session.prepare_numbered_capture((900, 600), html_enabled=True)
        page = Page(labels_after=3)
        with patch.object(session, "capture_map_image") as image:
            session._capture_best_numbered_view(
                page, TrackingConfig(), {"lat": 51, "lng": 31, "zoom": 7, "count": 3}
            )
        image.assert_not_called()
        self.assertIsNone(session.take_numbered_capture())
        self.assertEqual(page.zoom, 4)

        session.prepare_numbered_capture((900, 600), html_enabled=True)
        page = Page(labels_after=1)
        with patch.object(session, "capture_map_image", side_effect=[b"zoom-7", b"zoom-8"]):
            session._capture_numbered_views(page, TrackingConfig(), [
                {"lat": 51, "lng": 31, "zoom": 7},
                {"lat": 52, "lng": 32, "zoom": 8},
            ])
        captures = [session.take_numbered_capture(), session.take_numbered_capture()]
        self.assertEqual([capture.image for capture in captures], [b"zoom-7", b"zoom-8"])
        self.assertEqual([capture.html for capture in captures],
                         ["<html>zoom-7</html>", None])
        self.assertEqual(page.zoom, 4)

    def test_replayed_captures_travel_between_groups_at_overview_zoom(self) -> None:
        moves: list[tuple[float, float]] = []

        class Page:
            def evaluate(self, script: str, argument: object = None) -> object:
                if "const original =" in script:
                    return {"lat": 50, "lng": 0, "zoom": 10, "minZoom": 4}
                moves.append((argument["lng"], argument["zoom"]))
                return None

            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        session = BrowserSession(BrowserConfig())
        session.prepare_numbered_capture((900, 600))
        with patch.object(session, "_capture_visible_view", return_value=True):
            session._capture_numbered_views(Page(), TrackingConfig(), [
                {"lat": 50, "lng": 0, "zoom": 8},
                {"lat": 50, "lng": 12, "zoom": 8},
            ])
        self.assertEqual(moves, [
            (0, 4), (0, 8), (12, 4), (12, 8), (0, 4), (0, 10),
        ])

    def test_replayed_views_obey_zoom_limit_and_skip_captured_numbers(self) -> None:
        moves: list[tuple[float, float]] = []

        class Page:
            def evaluate(self, script: str, argument: object = None) -> object:
                if "const original =" in script:
                    return {"lat": 50, "lng": 0, "zoom": 4, "minZoom": 4}
                moves.append((argument["lng"], argument["zoom"]))
                return None

            def wait_for_timeout(self, _milliseconds: int) -> None:
                pass

        session = BrowserSession(BrowserConfig())
        session.prepare_numbered_capture((900, 600))
        with patch.object(session, "_capture_visible_view", return_value=True) as capture:
            session._capture_numbered_views(Page(), TrackingConfig(scan_max_zoom=8), [
                {"lat": 50, "lng": 0, "zoom": 8, "numbers": [101]},
                {"lat": 50, "lng": 2, "zoom": 8, "numbers": [101]},
                {"lat": 50, "lng": 4, "zoom": 9, "numbers": [102]},
            ])
        self.assertEqual(capture.call_count, 1)
        self.assertEqual(moves, [(0, 4), (0, 8), (0, 4), (0, 4)])

    def test_pair_capture_requires_target_number_and_restores_map(self) -> None:
        class Page:
            def __init__(self) -> None:
                self.restored = False
                self.fit = False
                self.labels = [87]

            def evaluate(self, script: str, argument: object = None) -> object:
                if "fitBounds !==" in script:
                    return {"lat": 0, "lng": 30, "zoom": 4}
                if "fitBounds(" in script:
                    self.fit = True
                    self.max_zoom = argument["maxZoom"]
                    return None
                if "querySelectorAll" in script:
                    return self.labels
                if "geo_source: 'leaflet'" in script:
                    return {"width": 1000, "height": 800, "zoom": 7,
                            "center": {"lat": 0, "lng": 1}, "has_leaflet": True}
                if "setView(" in script:
                    self.restored = True
                    return None
                if "getZoom()" in script:
                    return 7
                return None

            def wait_for_timeout(self, _milliseconds: int) -> None:
                return None

        page = Page()
        session = BrowserSession(BrowserConfig())
        session.prepare_numbered_capture((900, 600))
        with patch.object(session, "_find_page", return_value=page), \
                patch.object(session, "capture_map_image", return_value=b"jpeg"):
            self.assertTrue(session.capture_pair_view(
                87, (0, 0), (0, 1), TrackingConfig(),
            ))
        self.assertTrue(page.fit)
        self.assertEqual(page.max_zoom, 10)
        self.assertTrue(page.restored)
        self.assertEqual(session.take_numbered_capture().numbers, (87,))

        page = Page()
        page.labels = [99]
        session.prepare_numbered_capture((900, 600))
        with patch.object(session, "_find_page", return_value=page), \
                patch.object(session, "capture_map_image") as image:
            self.assertFalse(session.capture_pair_view(
                87, (0, 0), (0, 1), TrackingConfig(),
            ))
        image.assert_not_called()
        self.assertTrue(page.restored)

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
            session.numbered_capture = NumberedCapture(
                first, 7, b"jpeg-1", "<html>zoom-7</html>"
            )
            saved, errors = capture_due(session, settings)
            self.assertFalse(errors)
            self.assertEqual(len(saved), 2)
            self.assertEqual(
                period_path(settings.screenshot_dir, 3, first).name, "06-09"
            )
            self.assertIn("zoom-7", saved[0].name)
            self.assertIn("zoom-7", saved[1].name)
            self.assertEqual(saved[1].read_text(encoding="utf-8"), "<html>zoom-7</html>")
            second = datetime(2026, 10, 25, 8, 30, tzinfo=UTC)
            session.numbered_capture = NumberedCapture(
                second, 8, b"jpeg-2", "<html>zoom-8</html>"
            )
            repeated, errors = capture_due(session, settings)
            self.assertFalse(errors)
            self.assertEqual(len(repeated), 2)
            self.assertEqual(repeated[0].parent, saved[0].parent)
            self.assertEqual(repeated[1].parent, saved[1].parent)
            self.assertEqual(repeated[0].read_bytes(), b"jpeg-2")
            self.assertEqual(repeated[1].read_text(encoding="utf-8"), "<html>zoom-8</html>")
            self.assertEqual(capture_due(session, settings), ([], []))
            session.numbered_capture = NumberedCapture(
                second, 8, b"jpeg-3", "<html>same-time</html>"
            )
            duplicate_time, errors = capture_due(session, settings)
            self.assertFalse(errors)
            self.assertEqual(len(duplicate_time), 2)
            self.assertNotEqual(duplicate_time[0], repeated[0])
            self.assertNotEqual(duplicate_time[1], repeated[1])
            later_at = datetime(2026, 10, 25, 9, 2, tzinfo=UTC)
            session.numbered_capture = NumberedCapture(
                later_at, 9, b"jpeg-4", "<html>zoom-9</html>"
            )
            later, errors = capture_due(session, settings)
            self.assertFalse(errors)
            self.assertEqual(len(later), 2)
            self.assertNotEqual(later[0].parent, saved[0].parent)
            self.assertEqual(later[1].parent, saved[1].parent)

    def test_html_only_capture_uses_numbered_view(self) -> None:
        session = BrowserSession(BrowserConfig())
        session.prepare_numbered_capture(None, html_enabled=True)

        class Page:
            def evaluate(self, _script: str, _selector: str) -> int:
                return 1

            def content(self) -> str:
                return "<html>numbered</html>"

        with patch.object(session, "capture_map_image") as image:
            self.assertTrue(session._capture_mouse_numbered(
                Page(), {"map_zoom": 7}, ".leaflet-container"
            ))
        image.assert_not_called()
        capture = session.take_numbered_capture()
        self.assertIsNone(capture.image)
        self.assertEqual(capture.html, "<html>numbered</html>")

    def test_html_only_archive_is_written_each_scan(self) -> None:
        with TemporaryDirectory() as directory:
            settings = validate_settings(from_dict({
                "screenshot_enabled": False,
                "archive_html_enabled": True,
                "archive_html_dir": directory,
            }))
            session = FakeSession()
            captured_at = datetime(2026, 10, 25, 7, 15, tzinfo=UTC)
            for number in (1, 2):
                session.numbered_capture = NumberedCapture(
                    captured_at, 7, html=f"<html>{number}</html>"
                )
                saved, errors = capture_due(session, settings)
                self.assertFalse(errors)
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0].read_text(encoding="utf-8"),
                                 f"<html>{number}</html>")
            self.assertEqual(len(list(Path(directory).rglob("page_*.html"))), 2)

    def test_archive_reports_capture_error_without_a_view(self) -> None:
        settings = validate_settings(from_dict({
            "screenshot_enabled": True,
            "archive_html_enabled": True,
        }))
        session = FakeSession()
        session.numbered_capture_error = "снимок недоступен"
        session.numbered_html_error = "HTML недоступен"
        self.assertEqual(capture_due(session, settings), (
            [], ["map: снимок недоступен", "page: HTML недоступен"]
        ))

    def test_archive_settings_reject_unsupported_period_and_size(self) -> None:
        with self.assertRaises(ValueError):
            validate_settings(from_dict({"screenshot_period_hours": 2}))
        with self.assertRaises(ValueError):
            validate_settings(from_dict({"screenshot_width": 100}))


if __name__ == "__main__":
    unittest.main()
