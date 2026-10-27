"""Подключение Playwright к уже запущенному Chrome по CDP (не открывает новый браузер)."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Self

from parsing_site.cdp_check import cdp_endpoint_ok, format_cdp_help
from parsing_site.config import BrowserConfig, TrackingConfig
from parsing_site.tracking import extract_scene_with_mouse_scan

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright


_VISIBLE_NUMBERS_SCRIPT = """selector => {
  const map = document.querySelector(selector);
  if (!map) return [];
  const box = map.getBoundingClientRect();
  return [...map.querySelectorAll('.leaflet-marker-icon.map-incident-operator-number')]
    .filter(el => {
      if (!/^№?\\s*\\d+$/.test(el.textContent.trim())) return false;
      for (let node = el; node && node !== map; node = node.parentElement) {
        const style = getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden' ||
            Number(style.opacity) === 0) return false;
      }
      const rect = el.getBoundingClientRect();
      if (!rect.width || !rect.height) return false;
      const x = rect.left + rect.width / 2;
      const y = rect.top + rect.height / 2;
      return x >= box.left && x <= box.right && y >= box.top && y <= box.bottom;
    }).map(el => Number(el.textContent.trim().replace(/^№\\s*/, '')));
}"""


class BrowserNotConnectedError(RuntimeError):
    """Не удалось подключиться к уже запущенному браузеру по CDP."""


@dataclass(slots=True)
class PageSnapshot:
    """Минимальный снимок выбранной вкладки браузера."""

    url: str
    title: str
    html: str


@dataclass(slots=True)
class NumberedCapture:
    """Архивные данные одного вида карты с видимыми номерами."""

    captured_at: datetime
    zoom: float
    image: bytes | None = None
    html: str | None = None
    numbers: tuple[int, ...] = ()


class BrowserSession:
    """Подключение к уже открытому Chrome/Edge через Chrome DevTools Protocol."""

    def __init__(self, config: BrowserConfig) -> None:
        """Сохраняет параметры; подключение выполняется методом :meth:`open`."""
        self._config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._number_cache: dict[int, tuple[int, float, float, float]] = {}
        self._cache_day = datetime.now().astimezone().date()
        self._cache_url = ""
        self._numbered_size: tuple[int, int] | None = None
        self._numbered_html_enabled = False
        self._numbered_captures: list[NumberedCapture] = []
        self._on_numbered_capture: Callable[[NumberedCapture], None] | None = None
        self._numbered_html_captured = False
        self.numbered_capture_error = ""
        self.numbered_html_error = ""

    def prepare_numbered_capture(
        self, size: tuple[int, int] | None, html_enabled: bool = False,
        on_capture: Callable[[NumberedCapture], None] | None = None,
    ) -> None:
        """Готовит включённые архивы для очередного обхода карты."""
        self._numbered_size = size
        self._numbered_html_enabled = html_enabled
        self._numbered_captures = []
        self._on_numbered_capture = on_capture
        self._numbered_html_captured = False
        self.numbered_capture_error = ""
        self.numbered_html_error = ""

    def take_numbered_capture(self) -> NumberedCapture | None:
        """Возвращает архивный вид, полученный во время обхода."""
        return self._numbered_captures.pop(0) if self._numbered_captures else None

    def __enter__(self) -> Self:
        """Открывает CDP-сессию для использования класса как контекстного менеджера."""
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        """Освобождает Playwright, не закрывая пользовательский браузер."""
        self.close()

    def open(self) -> None:
        """Проверяет CDP endpoint и подключает синхронный Playwright."""
        from playwright.sync_api import sync_playwright

        ok, info = cdp_endpoint_ok(self._config.cdp_url)
        if not ok:
            raise BrowserNotConnectedError(format_cdp_help(self._config.cdp_url, info))

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(
                self._config.cdp_url
            )
        except Exception as exc:
            self.close()
            raise BrowserNotConnectedError(
                format_cdp_help(self._config.cdp_url, str(exc))
            ) from exc

    def close(self) -> None:
        """Останавливает Playwright, оставляя окно Chrome открытым."""
        # Не вызываем browser.close(): для CDP это закрыло бы окно пользователя.
        self._browser = None
        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None

    def capture(self) -> PageSnapshot:
        """Возвращает URL, заголовок и полный HTML выбранной вкладки."""
        page = self._ready_page()
        return PageSnapshot(
            url=page.url,
            title=page.title(),
            html=page.content(),
        )

    def capture_html(self) -> str:
        """Сохраняет DOM выбранной вкладки без ожидания наличия маркеров."""
        return self._find_page().content()

    def capture_scene(self, tracking: TrackingConfig) -> tuple[PageSnapshot, dict]:
        """HTML не качается целиком: в странице выполняется JS-съём маркеров и карты."""
        today = datetime.now().astimezone().date()
        if today != self._cache_day:
            self._number_cache.clear()
            self._cache_day = today
        waiter = ", ".join(
            part for part in (tracking.map_container_selector, self._config.wait_for_selector)
            if part.strip()
        )
        page = self._ready_page(waiter)
        if page.url != self._cache_url:
            self._number_cache.clear()
            self._cache_url = page.url
        snapshot = PageSnapshot(
            url=page.url,
            title=page.title(),
            html="",
        )
        raw = extract_scene_with_mouse_scan(
            page, tracking, self._number_cache,
            on_numbered_view=(
                lambda scene: self._capture_mouse_numbered(
                    page, scene, tracking.map_container_selector
                )
            ) if self._numbered_size or self._numbered_html_enabled else None,
        )
        scan = raw.get("scan") or {}
        views = scan.get("numbered_views") or []
        if (self._numbered_size or self._numbered_html_enabled) and views:
            self._capture_numbered_views(page, tracking, views)
        return snapshot, raw

    def _capture_mouse_numbered(self, page: Page, scene: dict, selector: str) -> bool:
        """Снимает архив прямо во время приближения мышью."""
        if self._numbered_size is None and self._numbered_html_captured:
            return True
        zoom = scene.get("map_zoom")
        if zoom is None:
            return False
        return self._capture_visible_view(page, selector, float(zoom))

    def _capture_visible_view(
        self, page: Page, selector: str, zoom: float
    ) -> bool:
        """Берёт HTML и JPEG только при наличии видимой подписи в DOM."""
        try:
            labels = page.evaluate(_VISIBLE_NUMBERS_SCRIPT, selector)
            if not labels:
                return False
        except Exception as exc:  # noqa: BLE001 — ошибка архива не прерывает обход.
            if self._numbered_size is not None:
                self.numbered_capture_error = str(exc)
            if self._numbered_html_enabled:
                self.numbered_html_error = str(exc)
            return False
        capture = NumberedCapture(datetime.now().astimezone(), zoom)
        if isinstance(labels, list):
            capture.numbers = tuple(sorted({int(number) for number in labels if int(number) > 0}))
        if self._numbered_html_enabled and not self._numbered_html_captured:
            try:
                capture.html = page.content()
                self._numbered_html_captured = True
            except Exception as exc:  # noqa: BLE001 — один архив не блокирует другой.
                self.numbered_html_error = str(exc)
        if self._numbered_size is not None:
            try:
                capture.image = self.capture_map_image(selector, *self._numbered_size)
            except Exception as exc:  # noqa: BLE001 — один архив не блокирует другой.
                self.numbered_capture_error = str(exc)
        if capture.image is not None or capture.html is not None:
            if self._on_numbered_capture is not None:
                self._on_numbered_capture(capture)
            else:
                self._numbered_captures.append(capture)
        return True

    def _capture_best_numbered_view(self, page: Page, tracking: TrackingConfig, best: dict) -> None:
        """Совместимый вызов съёмки одного подписанного вида Leaflet."""
        self._capture_numbered_views(page, tracking, [best])

    def _capture_numbered_views(
        self, page: Page, tracking: TrackingConfig, views: list[dict]
    ) -> None:
        """Снимает каждый подписанный вид Leaflet и восстанавливает исходный центр."""
        if self._numbered_size is None and not self._numbered_html_enabled:
            return
        original = None
        try:
            original = page.evaluate(
                """() => {
                  const map = window.__parsingSiteMap;
                  if (!map || typeof map.setView !== 'function') return null;
                  const center = map.getCenter();
                  const original = {lat: center.lat, lng: center.lng, zoom: map.getZoom()};
                  return original;
                }""",
            )
            if original is None:
                return
            for view in views:
                try:
                    page.evaluate(
                        """view => window.__parsingSiteMap.setView(
                          [view.lat, view.lng], view.zoom, {animate: false}
                        )""",
                        view,
                    )
                    for _ in range(2):
                        page.wait_for_timeout(round(tracking.scan_wait_seconds * 1000))
                        if self._capture_visible_view(
                            page, tracking.map_container_selector,
                            float(view["zoom"]),
                        ):
                            break
                except Exception as exc:  # noqa: BLE001 — остальные виды ещё можно снять.
                    self.numbered_capture_error = str(exc)
                if self._numbered_size is None and self._numbered_html_captured:
                    break
        except Exception as exc:  # noqa: BLE001 — ошибка архива не прерывает обход.
            self.numbered_capture_error = str(exc)
        finally:
            if original is not None:
                try:
                    page.evaluate(
                        """view => window.__parsingSiteMap.setView(
                          [view.lat, view.lng], view.zoom, {animate: false}
                        )""",
                        original,
                    )
                    page.wait_for_timeout(round(tracking.scan_wait_seconds * 1000))
                except Exception as exc:  # noqa: BLE001 — исходный вид важнее снимка.
                    self.numbered_capture_error = f"Не удалось восстановить карту: {exc}"

    def capture_map_image(self, selector: str, max_width: int, max_height: int) -> bytes:
        """Снимок контейнера карты в JPEG, вписанный в заданный размер."""
        page = self._find_page()
        png = page.locator(selector).first.screenshot(type="png", animations="disabled")
        encoded = base64.b64encode(png).decode("ascii")
        result = page.evaluate(
            """async ({encoded, maxWidth, maxHeight}) => {
              const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
              const image = await createImageBitmap(new Blob([bytes], {type: 'image/png'}));
              const scale = Math.min(1, maxWidth / image.width, maxHeight / image.height);
              const canvas = document.createElement('canvas');
              canvas.width = Math.max(1, Math.round(image.width * scale));
              canvas.height = Math.max(1, Math.round(image.height * scale));
              canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
              image.close();
              return canvas.toDataURL('image/jpeg', 0.78).split(',')[1];
            }""",
            {"encoded": encoded, "maxWidth": max_width, "maxHeight": max_height},
        )
        return base64.b64decode(result)

    def list_tabs(self) -> list[tuple[str, str]]:
        """Перечисляет URL и заголовки всех вкладок во всех контекстах."""
        browser = self._require_browser()
        tabs: list[tuple[str, str]] = []
        for context in browser.contexts:
            for page in context.pages:
                tabs.append((page.url, page.title()))
        return tabs

    def _ready_page(self, waiter_override: str | None = None) -> Page:
        """Выбирает вкладку и при необходимости ждёт обязательный CSS-селектор."""
        page = self._find_page()
        waiter = (waiter_override or self._config.wait_for_selector).strip()
        if waiter:
            from playwright.sync_api import TimeoutError as PlaywrightTimeout

            try:
                page.wait_for_selector(waiter, timeout=self._config.wait_timeout_ms)
            except PlaywrightTimeout as exc:
                raise RuntimeError(
                    f"На странице не появился селектор '{waiter}' "
                    f"(таймаут {self._config.wait_timeout_ms} мс)."
                ) from exc
        return page

    def _find_page(self) -> Page:
        """Находит вкладку по подстроке URL или возвращает первую доступную."""
        browser = self._require_browser()
        pages: list[Page] = [
            page for context in browser.contexts for page in context.pages
        ]
        if not pages:
            raise RuntimeError(
                "В браузере нет открытых вкладок. Откройте нужный сайт вручную."
            )

        needle = self._config.url_contains.strip().lower()
        if not needle:
            return pages[0]

        for page in pages:
            if needle in page.url.lower():
                return page

        available = "\n".join(f"  - {page.url}" for page in pages)
        raise RuntimeError(
            f"Не найдена вкладка с URL, содержащим '{self._config.url_contains}'.\n"
            f"Открытые вкладки:\n{available}"
        )

    def _require_browser(self) -> Browser:
        """Возвращает активное CDP-подключение либо сообщает об ошибке состояния."""
        if self._browser is None:
            raise BrowserNotConnectedError("Сессия браузера не открыта.")
        return self._browser
