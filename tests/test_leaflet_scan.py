from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from parsing_site.config import TrackingConfig
from parsing_site.coordinates import (
    MapView,
    container_to_latlng,
    latlng_to_container,
    project_3857,
    unproject_3857,
)
from parsing_site.html_probe import extract_scene_from_html
from parsing_site.tracking import _EXTRACT_SCRIPT, extract_scene_with_mouse_scan


class LeafletScanTest(unittest.TestCase):
    def test_saved_zoom_views_have_objects_and_then_numbers(self) -> None:
        root = Path(__file__).resolve().parent.parent / "html_test"
        overview = root / "1.html"
        numbered = root / "2 (1).html"
        if not overview.exists() or not numbered.exists():
            self.skipTest("пара HTML-снимков пользователя отсутствует")
        config = TrackingConfig(dynamic_selector=".leaflet-marker-icon.map-incident")
        low = extract_scene_from_html(overview.read_text(encoding="utf-8"), config)
        high = extract_scene_from_html(numbered.read_text(encoding="utf-8"), config)
        self.assertEqual(low["map_zoom"], 5)
        self.assertEqual(high["map_zoom"], 7)
        self.assertEqual(len(low["dynamics"]), 384)
        self.assertEqual(low["unnumbered_dynamics"], 384)
        self.assertEqual(len(high["dynamics"]), 161)
        self.assertEqual(high["unnumbered_dynamics"], 0)

    def test_overview_and_numbered_grid_scan(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js недоступен")
        script = Path(__file__).with_name("leaflet_scan_check.js")
        with tempfile.TemporaryDirectory() as directory:
            extract_path = Path(directory) / "extract.js"
            extract_path.write_text(f"({_EXTRACT_SCRIPT})", encoding="utf-8")
            result = subprocess.run(
                [node, str(script), str(extract_path)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_browser_wheel_fallback_assigns_numbers_and_restores_view(self) -> None:
        page = _FakePage()
        cache: dict[int, tuple[int, float, float, float]] = {}
        with patch("parsing_site.tracking.extract_scene_from_page", side_effect=lambda _p, _t, **_kw: page.scene()):
            result = extract_scene_with_mouse_scan(page, TrackingConfig(), cache)
            moves = page.mouse.wheel_calls
            cached_result = extract_scene_with_mouse_scan(page, TrackingConfig(), cache)
        self.assertEqual(result["scan"]["status"], "complete")
        self.assertEqual([item["index"] for item in result["dynamics"]], [101, 102, 103])
        self.assertEqual(result["unnumbered_dynamics"], 0)
        self.assertEqual(cached_result["scan"]["status"], "cached")
        self.assertEqual(cached_result["unnumbered_dynamics"], 0)
        self.assertEqual(page.mouse.wheel_calls, moves)
        self.assertEqual(page.zoom, 5)
        self.assertAlmostEqual(page.center_lng, 40, places=5)
        self.assertGreater(page.mouse.zoom_in_calls, 0)
        self.assertEqual(page.waits.count(1000), page.mouse.zoom_in_calls)

    def test_browser_wheel_retries_unlabelled_object_in_crowd(self) -> None:
        page = _FakePage(crowded=True)
        with patch(
            "parsing_site.tracking.extract_scene_from_page",
            side_effect=lambda _p, _t, **_kw: page.scene(),
        ):
            result = extract_scene_with_mouse_scan(page, TrackingConfig())
        self.assertEqual(
            [item["index"] for item in result["dynamics"]], [101, 102, 103]
        )
        self.assertEqual(result["scan"]["status"], "complete")
        self.assertEqual(page.zoom, 5)
        self.assertGreater(page.mouse.zoom_in_calls, 0)
        self.assertEqual(page.waits.count(1000), page.mouse.zoom_in_calls)


class _FakeMouse:
    def __init__(self, page: _FakePage) -> None:
        self.page = page
        self.x = 400.0
        self.y = 300.0
        self.wheel_calls = 0
        self.zoom_in_calls = 0

    def move(self, x: float, y: float) -> None:
        self.x, self.y = x, y

    def wheel(self, _dx: int, dy: int) -> None:
        self.wheel_calls += 1
        self.zoom_in_calls += dy < 0
        page = self.page
        view = page.view()
        anchor = container_to_latlng(self.x, self.y, view)
        assert anchor is not None
        page.zoom += 1 if dy < 0 else -1
        world_x, world_y = project_3857(*anchor, page.zoom)
        page.center_lat, page.center_lng = unproject_3857(
            world_x - (self.x - 400), world_y - (self.y - 300), page.zoom
        )


class _FakePage:
    def __init__(self, crowded: bool = False) -> None:
        self.zoom = 5
        self.crowded = crowded
        self.center_lat = 50.0
        self.center_lng = 40.0
        self.mouse = _FakeMouse(self)
        self.waits: list[int] = []

    def wait_for_timeout(self, milliseconds: int) -> None:
        self.waits.append(milliseconds)

    def view(self) -> MapView:
        return MapView(
            width=800, height=600, zoom=self.zoom,
            center_lat=self.center_lat, center_lng=self.center_lng,
        )

    def scene(self) -> dict:
        view = self.view()
        dynamics = []
        for i, lng in enumerate((36.0, 40.0, 44.0)):
            point = latlng_to_container(50.0, lng, view)
            assert point is not None
            if 0 <= point[0] <= 800 and 0 <= point[1] <= 600:
                number_visible = self.zoom >= (
                    9 if self.crowded and i == 1 else 7
                )
                dynamics.append({
                    "index": 101 + i if number_visible else -101 - i,
                    "fallback_id": -101 - i,
                    "lat": 50.0, "lng": lng,
                    "x": point[0], "y": point[1],
                })
        proxy_x, proxy_y = project_3857(self.center_lat, self.center_lng, self.zoom)
        return {
            "dynamics": dynamics,
            "unnumbered_dynamics": sum(item["index"] < 0 for item in dynamics),
            "map_zoom": self.zoom,
            "map": {
                "width": 800, "height": 600, "left": 0, "top": 0,
                "zoom": self.zoom, "geo_source": "proxy3857",
                "proxy": {"x": proxy_x, "y": proxy_y, "scale": 2 ** (self.zoom - 1)},
                "center": {"lat": self.center_lat, "lng": self.center_lng},
            },
            "scan": {"status": "no_leaflet_instance"},
        }
