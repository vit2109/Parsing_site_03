"""Ручная браузерная проверка: python tests/history_map_browser_check.py."""

from __future__ import annotations

import json
import base64
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
STATIC = {
    "static_index": 1, "static_name": "Точка Москва",
    "static_lat": 55.7558, "static_lng": 37.6173,
    "lat": 55.7558, "lng": 37.6173,
    "alert_radius_km": 1, "warning_radius_km": 3, "blue_radius_km": 6,
}
POINTS = [
    {"lat": 55.76 + i * 0.01, "lng": 37.62 + i * 0.02,
     "ts": f"2026-10-15T10:0{i}:00+03:00", "distance": i + 1,
     "status": "warning", "dynamic_index": 72, "dynamic_name": "БПЛА",
     "object_date": "2026-10-15"}
    for i in range(3)
]


def run() -> None:
    handler = partial(SimpleHTTPRequestHandler, directory=str(ROOT / "web"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(
                viewport={"width": 1440, "height": 1000}, ignore_https_errors=True
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            tile = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lXcAAAAASUVORK5CYII="
            )
            page.route("https://tile.openstreetmap.org/**", lambda route: route.fulfill(
                body=tile, content_type="image/png"
            ))

            def api(route):
                path = route.request.url.split("/api/", 1)[1].split("?", 1)[0]
                if path == "history/static-objects":
                    data = [STATIC]
                elif path == "settings":
                    data = {"blue_color": "#2878b8"}
                elif path.startswith("history/static-zones/"):
                    data = {
                        **STATIC, "objects_total": 1, "points_total": 3,
                        "trajectories": [{"dynamic_index": 72,
                            "dynamic_name": "БПЛА", "object_date": "2026-10-15",
                            "trajectory_key": "2026-10-15:72", "color": "#ff6b6b",
                            "entered_warning": True, "points": POINTS}],
                    }
                elif path.startswith("history/trajectory/"):
                    data = {"dynamic_index": 72, "dynamic_name": "БПЛА",
                            "object_date": "2026-10-15", "total": 3,
                            "color": "#ff6b6b", "points": POINTS}
                else:
                    raise AssertionError(f"Unexpected API: {path}")
                route.fulfill(content_type="application/json", body=json.dumps(data))

            page.route("**/api/**", api)
            page.goto(f"http://127.0.0.1:{server.server_port}/templates/history.html")
            overview = page.locator("#history-overview-map")
            expect(overview.locator(".leaflet-container")).to_be_visible()
            overview.get_by_role("combobox").select_option("offline")
            expect(overview.locator(".offline-map-svg")).to_be_visible()
            overview.get_by_role("combobox").select_option("geographic")
            surface = overview.locator(".leaflet-container")
            size = surface.bounding_box()
            assert size is not None
            surface.click(position={"x": size["width"] / 2, "y": size["height"] / 2})
            modal = page.locator("#static-modal")
            expect(modal.locator(".leaflet-container")).to_be_visible()
            modal.locator("[data-zone-trajectory]").click()
            modal.locator("[data-zone-point]").first.click()
            expect(modal.locator(".leaflet-popup-content")).to_contain_text("БПЛА")
            modal.get_by_role("combobox").select_option("offline")
            expect(modal.locator(".offline-map-selection")).to_contain_text("БПЛА")
            modal.get_by_role("combobox").select_option("geographic")
            expect(modal.locator(".leaflet-popup-content")).to_contain_text("БПЛА")
            page.wait_for_function("""() => {
                const tiles = [...document.querySelectorAll('#static-modal img.leaflet-tile')];
                return tiles.length > 0 && tiles.every(i => i.complete && i.naturalWidth > 0);
            }""", timeout=20000)
            (ROOT / "artifacts").mkdir(exist_ok=True)
            page.screenshot(path=str(ROOT / "artifacts/history-geographic-map.png"))
            modal.get_by_role("button", name="Закрыть", exact=True).click()
            page.evaluate("openDynamicTrajectory(72, '2026-10-15')")
            trajectory = page.locator("#trajectory-modal")
            expect(trajectory.locator(".leaflet-container")).to_be_visible()
            trajectory.locator("[data-point-index]").first.click()
            expect(trajectory.locator(".leaflet-popup-content")).to_be_visible()
            trajectory.get_by_role("button", name="Закрыть", exact=True).click()
            page.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
            page.evaluate("staticObjects[0].static_lat = 60; staticObjects[0].static_lng = 80; renderOverviewMap()")
            expect(overview.locator(".geographic-map-message")).to_be_visible()
            overview.get_by_role("combobox").select_option("offline")
            expect(overview.locator(".offline-map-svg")).to_be_visible()
            assert not errors, errors
            browser.close()
            print("Geographic map: overview, zones, trajectories, switching and tile failure passed")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    run()
