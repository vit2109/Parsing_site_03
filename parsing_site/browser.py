"""Подключение Playwright к уже запущенному Chrome по CDP (не открывает новый браузер)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Self

from parsing_site.cdp_check import cdp_endpoint_ok, format_cdp_help
from parsing_site.config import BrowserConfig, TrackingConfig
from parsing_site.tracking import extract_scene_with_mouse_scan

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright


class BrowserNotConnectedError(RuntimeError):
    """Не удалось подключиться к уже запущенному браузеру по CDP."""


@dataclass(slots=True)
class PageSnapshot:
    """Минимальный снимок выбранной вкладки браузера."""

    url: str
    title: str
    html: str


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
        return snapshot, extract_scene_with_mouse_scan(page, tracking, self._number_cache)

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
