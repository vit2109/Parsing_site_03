"""Сквозная проверка интерфейса и подхвата внешнего сервера без рабочего сайта."""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["PARSING_SITE_INSTANCE"] = "qa_performance"

import launcher
from parsing_site.config import StaticObjectConfig, TrackingConfig
from parsing_site.settings_store import from_dict
from parsing_site.tracking import (
    build_tracking_snapshot,
    snapshot_to_dict,
)
from parsing_site.webapp import app, store, worker


def run() -> None:
    output = ROOT / "artifacts" / "performance-check"
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    app.state.uvicorn_server = server
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    base = f"http://127.0.0.1:{port}"
    try:
        settings = from_dict({"site": "https://demo.example/map", "interval_seconds": 60,
                              "static_objects": [{"name": f"Точка {i}", "lat": 55 + i * .1,
                                                  "lng": 37 + i * .1} for i in range(1, 11)]})
        store.save(settings)
        tracking = TrackingConfig(coordinate_mode="latlng", static_objects=[
            StaticObjectConfig(i, f"Точка {i}", 0, 0, 55 + i * .1, 37 + i * .1)
            for i in range(1, 11)
        ])
        snapshot = build_tracking_snapshot(url=settings.site, title="Демонстрационная карта",
            tracking=tracking, raw={"coordinate_mode": "latlng", "dynamics": [
                {"index": i, "name": f"Объект {i}", "x": 0, "y": 0,
                 "lat": 55 + i * .002, "lng": 37 + i * .003} for i in range(1, 151)
            ]})
        with worker._lock:
            worker._latest = snapshot_to_dict(snapshot)
            worker._revision += 1
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1050}, device_scale_factor=1)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base)
            page.wait_for_function("document.querySelector('#stat-dyn').textContent === '150'")
            assert page.locator(".topbar__actions #btn-test-html").count() == 0
            assert page.locator(".topbar__actions a[href='/max-logs']").count() == 0
            page.locator("#btn-speedup").click()
            page.wait_for_function("document.querySelector('#btn-speedup').getAttribute('aria-pressed') === 'true'")
            assert store.current().speedup_enabled
            page.locator("#btn-speedup").click()
            page.wait_for_function("document.querySelector('#btn-speedup').getAttribute('aria-pressed') === 'false'")
            page.locator("[data-toggle]").first.click()
            page.wait_for_timeout(2200)
            assert page.locator(".group-toggle").first.get_attribute("aria-expanded") == "true"
            page.locator("[data-toggle]").first.click()
            page.screenshot(path=str(output / "main.png"), full_page=True)
            page.locator("#btn-settings").click()
            page.locator("#tab-service").click()
            page.locator("[name=history_keep_days]").fill("45")
            page.locator("[name=speedup_disable_html]").uncheck()
            page.locator("#settings-dialog").screenshot(path=str(output / "service.png"))
            page.locator("button[value=save]").click()
            page.wait_for_timeout(700)
            if page.locator("#settings-dialog").evaluate("el => el.open"):
                print("Form error:", page.locator("#settings-error").text_content(), flush=True)
                print("Invalid fields:", page.locator("#settings-form").evaluate(
                    "el => [...el.elements].filter(f => f.willValidate && !f.checkValidity()).map(f => [f.name,f.value,f.validationMessage])"), flush=True)
            page.wait_for_function("!document.querySelector('#settings-dialog').open")
            assert store.current().history_keep_days == 45
            assert not store.current().speedup_disable_html
            page.reload()
            page.locator("#btn-settings").click()
            page.locator("#tab-service").click()
            assert page.locator("[name=history_keep_days]").input_value() == "45"
            assert not page.locator("[name=speedup_disable_html]").is_checked()
            page.locator("#btn-cancel").click()
            with worker._lock:
                worker._latest = {**worker._latest, "in_progress": True}
                worker._revision += 1
            page.wait_for_function("document.querySelector('#table-meta').textContent.includes('Обход выполняется')")
            assert not errors, errors
            browser.close()

        try:
            urllib.request.urlopen(urllib.request.Request(base + "/api/history/prune?keep_days=19", method="POST"))
        except urllib.error.HTTPError as error:
            assert error.code == 422
        else:
            raise AssertionError("API accepted retention below 20 days")
        registry = ROOT / "data" / f"launcher-{port}.json"
        assert registry.exists()
        managed = launcher.discover_target(ROOT / "main.py", launcher.Target("Внешний запуск", port, 9222))
        assert managed is not None and managed.process is None
        assert managed.target.instance == "qa_performance"
        launcher.stop_target(managed)
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not registry.exists()
        print(json.dumps({"browser_errors": errors, "objects": 150, "rows": len(snapshot.rows),
                          "standalone_adoption_and_shutdown": True, "retention_minimum": 20}))
    finally:
        server.should_exit = True
        thread.join(timeout=10)


if __name__ == "__main__":
    run()
