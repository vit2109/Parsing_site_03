"""Снимки настоящей карты хронологии для справки Word."""

from __future__ import annotations

import mimetypes
from math import ceil
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Route, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from parsing_site.config import WEB_DIR
from parsing_site.coordinates import haversine_km

_ORIGIN = "http://history-report.local"
def _sample(items: list[dict[str, Any]], limit: int = 1_500) -> list[dict[str, Any]]:
    if len(items) <= limit:
        return items
    step = ceil((len(items) - 1) / (limit - 1))
    return [items[index] for index in range(0, len(items) - 1, step)] + [items[-1]]


def _nearby_statics(
    points: list[dict[str, Any]], statics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    route = _sample([point for point in points if point.get("lat") is not None
                     and point.get("lng") is not None], 100)
    if not route:
        return []
    nearby = []
    for static in statics:
        lat, lng = static.get("static_lat"), static.get("static_lng")
        if lat is None or lng is None:
            continue
        distance = min(haversine_km(float(lat), float(lng), float(point["lat"]),
                                    float(point["lng"])) for point in route)
        if distance <= 150:
            nearby.append((distance, static))
    return [static for _, static in sorted(nearby, key=lambda item: item[0])[:100]]


def _serve_report_asset(route: Route) -> None:
    path = urlsplit(route.request.url).path
    if path == "/history":
        target = WEB_DIR / "templates" / "history.html"
    elif path.startswith("/static/"):
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if not target.is_relative_to((WEB_DIR / "static").resolve()):
            route.abort()
            return
    elif path.startswith("/api/"):
        route.fulfill(status=200, content_type="application/json", body="[]")
        return
    else:
        route.abort()
        return
    if not target.is_file():
        route.abort()
        return
    route.fulfill(path=target, content_type=mimetypes.guess_type(target.name)[0] or "application/octet-stream")


def _capture_one(page: Any, kind: str, payload: dict[str, Any], mode: str) -> bytes:
    page.evaluate("mode => localStorage.setItem('history-map-mode', mode)", mode)
    rendered = page.evaluate("args => window.renderReportMap(args.kind, args.payload)",
                             {"kind": kind, "payload": payload})
    if not rendered:
        raise RuntimeError(f"Карта справки {kind} не нарисована")
    page.locator("#report-map-capture .history-map-surface").wait_for(state="visible")
    if mode == "geographic":
        try:
            page.wait_for_function(
                """() => {
                    const tiles = [...document.querySelectorAll('#report-map-capture .leaflet-tile')];
                    return tiles.length > 0 && tiles.every(tile => tile.complete && tile.naturalWidth > 0);
                }""",
                timeout=5_000,
            )
            page.wait_for_timeout(350)
        except PlaywrightTimeoutError:
            page.locator("#report-map-capture select").select_option("offline")
    return page.locator("#report-map-capture").screenshot(type="png", animations="disabled")


def capture_history_maps(
    *, static_scene: dict[str, Any] | None,
    dynamic_points: list[dict[str, Any]] | None,
    statics: list[dict[str, Any]] | None,
    rows: list[dict[str, Any]],
    map_mode: str = "geographic",
) -> dict[str, bytes]:
    """Рисует выбранные карты тем же JS, что виден в разделе «Хронология»."""
    jobs: list[tuple[str, dict[str, Any]]] = []
    if static_scene and static_scene.get("lat") is not None and static_scene.get("lng") is not None:
        jobs.append(("static", {"scene": {**static_scene, "points": _sample(static_scene.get("points") or [])}}))
    if dynamic_points:
        jobs.append(("dynamic", {"points": _sample(dynamic_points),
                                 "statics": _nearby_statics(dynamic_points, statics or [])}))
    if not jobs and rows:
        jobs.append(("overview", {"rows": _sample(rows)}))
    if not jobs:
        return {}

    images: dict[str, bytes] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True, timeout=12_000)
        try:
            page = browser.new_page(viewport={"width": 1500, "height": 1000}, device_scale_factor=1.5)
            page.route(f"{_ORIGIN}/**", _serve_report_asset)
            page.goto(f"{_ORIGIN}/history", wait_until="domcontentloaded", timeout=12_000)
            page.wait_for_function("typeof renderHistoryMap === 'function'", timeout=12_000)
            page.add_script_tag(path=str(WEB_DIR / "static" / "js" / "history-report-capture.js"))
            for kind, payload in jobs:
                images[kind] = _capture_one(page, kind, payload, map_mode)
        finally:
            browser.close()
    return images
