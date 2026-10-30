"""Ручная проверка полного охвата: python -m tests.simulator_browser_check [1000]."""

from __future__ import annotations

import sys
import socket
import threading
import time

from playwright.sync_api import sync_playwright
import uvicorn

from parsing_site.webapp import app


def run(count: int = 150) -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "Локальный сервер тренажёра не запустился"
    url = f"http://127.0.0.1:{listener.getsockname()[1]}/simulator"
    try:
        check_browser(url, count)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def check_browser(url: str, count: int) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("https://tile.openstreetmap.org/**", lambda route: route.abort())
        page.goto(url)
        page.locator("#sim-step").wait_for(state="visible")
        page.locator("#sim-count").fill(str(count))
        page.locator("#sim-area").select_option("mixed")
        page.locator("#sim-mode").select_option("full_coverage")
        page.locator("#sim-generate").click()
        try:
            page.wait_for_function("count => document.querySelector('#sim-route-count').textContent.startsWith(count)", arg=str(count), timeout=20000)
        except Exception:
            print("error:", page.locator("#sim-error").text_content())
            print("route:", page.locator("#sim-route-count").text_content())
            print("page errors:", errors)
            raise
        result = page.evaluate("""maxClicks => {
          const step = document.querySelector('#sim-step');
          let clicks = 0;
          while (Number(document.querySelector('#sim-missing').textContent) > 0 && clicks < maxClicks) {
            step.click();
            clicks++;
          }
          step.click();
          return { clicks, found: document.querySelector('#sim-found').textContent,
            missing: document.querySelector('#sim-missing').textContent,
            progress: document.querySelector('#sim-progress').textContent };
        }""", count + 1)
        assert result["found"] == str(count) and result["missing"] == "0", result
        assert "исходный вид восстановлен" in result["progress"], result
        assert not errors, errors
        print({key: result[key] for key in ("clicks", "found", "missing")})
        browser.close()


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 150)
