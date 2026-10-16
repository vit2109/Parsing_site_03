"""Настройки из SQLite (перекрывают config.toml, пока пользователь не сбросит БД)."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from parsing_site.config import AppConfig, StaticObjectConfig, load_config
from parsing_site.coordinates import normalize_mode
from parsing_site.db import Database

_HEX = re.compile(r"^#([0-9a-fA-F]{6})$")
COORD_MODES = {"screen", "map_relative", "percent", "latlng"}


@dataclass(slots=True)
class RuntimeSettings:
    """Настройки, изменяемые пользователем без перезапуска приложения."""

    site: str = "http://127.0.0.1:8000"
    cdp_url: str = "http://127.0.0.1:9222"
    interval_seconds: float = 2.0
    threshold: float = 500.0
    warning_distance: float = 1000.0
    blue_distance: float = 350.0
    alert_color: str = "#9b1c1c"
    warning_color: str = "#d4a017"
    blue_color: str = "#2878b8"
    sound_enabled: bool = True
    wait_for_selector: str = ".leaflet-marker-icon.map-incident"
    dynamic_selector: str = ".leaflet-marker-icon.map-incident"
    coordinate_mode: str = "latlng"
    map_coordinate_precision: int = 4
    map_container_selector: str = ".leaflet-container"
    reference_zoom: float = 0.0
    lock_scale: bool = False
    static_source: str = "config"
    static_objects: list[StaticObjectConfig] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Сериализует настройки и вложенные точки в JSON-совместимый словарь."""
        payload = asdict(self)
        payload["static_objects"] = [asdict(item) for item in self.static_objects]
        return payload

    def to_app_config(self, base: AppConfig | None = None) -> AppConfig:
        """Накладывает runtime-настройки на полную конфигурацию приложения."""
        cfg = base or load_config()
        warning_ratio = (
            self.warning_distance / self.threshold if self.threshold else 1.4
        )
        return replace(
            cfg,
            browser=replace(
                cfg.browser,
                cdp_url=self.cdp_url,
                url_contains=self.site,
                wait_for_selector=self.wait_for_selector,
            ),
            schedule=replace(cfg.schedule, interval_seconds=self.interval_seconds),
            tracking=replace(
                cfg.tracking,
                dynamic_selector=self.dynamic_selector,
                threshold=self.threshold,
                warning_ratio=max(1.0, warning_ratio),
                warning_distance=self.warning_distance,
                blue_distance=self.blue_distance,
                alert_color=self.alert_color,
                warning_color=self.warning_color,
                blue_color=self.blue_color,
                static_source=self.static_source,
                coordinate_mode=self.coordinate_mode,
                map_container_selector=self.map_container_selector,
                reference_zoom=self.reference_zoom,
                lock_scale=self.lock_scale,
                static_objects=list(self.static_objects),
            ),
        )


class SettingsStore:
    """Хранит runtime-настройки одной JSON-записью в основной БД."""

    KEY = "runtime"

    def __init__(self, db: Database) -> None:
        """Связывает хранилище настроек с открытой основной БД."""
        self._db = db

    def ensure_defaults(self, base: AppConfig | None = None) -> RuntimeSettings:
        """Возвращает настройки или впервые сохраняет значения из TOML."""
        current = self.load()
        if current is not None:
            return current
        settings = from_app_config(base or load_config())
        self.save(settings)
        return settings

    def load(self) -> RuntimeSettings | None:
        """Читает и десериализует настройки, если они уже сохранены."""
        raw = self._db.get_setting(self.KEY)
        if not raw:
            return None
        return from_dict(json.loads(raw))

    def current(self, base: AppConfig | None = None) -> RuntimeSettings:
        """Возвращает актуальные настройки, при необходимости создавая их."""
        return self.load() or self.ensure_defaults(base)

    def save(self, settings: RuntimeSettings) -> RuntimeSettings:
        """Валидирует, сериализует и сохраняет настройки."""
        cleaned = validate_settings(settings)
        self._db.set_setting(
            self.KEY, json.dumps(cleaned.to_dict(), ensure_ascii=False)
        )
        return cleaned


def from_app_config(cfg: AppConfig) -> RuntimeSettings:
    """Создаёт изменяемые настройки из полной TOML-конфигурации."""
    warning = cfg.tracking.warning_distance or cfg.tracking.warning_limit
    site = cfg.browser.url_contains or "http://127.0.0.1:8000"
    if site.isdigit():
        site = f"http://127.0.0.1:{site}"
    return RuntimeSettings(
        site=site,
        cdp_url=cfg.browser.cdp_url,
        interval_seconds=cfg.schedule.interval_seconds,
        threshold=cfg.tracking.threshold,
        warning_distance=warning,
        blue_distance=cfg.tracking.blue_limit,
        alert_color=cfg.tracking.alert_color,
        warning_color=cfg.tracking.warning_color,
        blue_color=cfg.tracking.blue_color,
        sound_enabled=True,
        wait_for_selector=cfg.browser.wait_for_selector
        or cfg.tracking.dynamic_selector,
        dynamic_selector=cfg.tracking.dynamic_selector,
        coordinate_mode=cfg.tracking.coordinate_mode,
        map_container_selector=cfg.tracking.map_container_selector,
        reference_zoom=cfg.tracking.reference_zoom,
        lock_scale=cfg.tracking.lock_scale,
        static_source=cfg.tracking.static_source,
        static_objects=list(cfg.tracking.static_objects),
    )


def from_dict(data: dict[str, Any]) -> RuntimeSettings:
    """Десериализует словарь API/SQLite в runtime-настройки."""
    threshold = float(data.get("threshold", 500))
    warning_distance = max(float(data.get("warning_distance", 1000)), threshold)
    blue_distance = max(float(data.get("blue_distance", 350)), warning_distance)
    objects: list[StaticObjectConfig] = []
    for index, item in enumerate(data.get("static_objects") or [], start=1):
        objects.append(
            StaticObjectConfig(
                id=int(item.get("id", index)),
                name=str(item.get("name", f"Статичный {index}")),
                x=float(item.get("x", 0)),
                y=float(item.get("y", 0)),
                lat=_optional(item.get("lat")),
                lng=_optional(item.get("lng")),
            )
        )
    return RuntimeSettings(
        site=str(data.get("site", "http://127.0.0.1:8000")),
        cdp_url=str(data.get("cdp_url", "http://127.0.0.1:9222")),
        interval_seconds=float(data.get("interval_seconds", 2)),
        threshold=threshold,
        warning_distance=warning_distance,
        blue_distance=blue_distance,
        alert_color=str(data.get("alert_color", "#9b1c1c")),
        warning_color=str(data.get("warning_color", "#d4a017")),
        blue_color=str(data.get("blue_color", "#2878b8")),
        sound_enabled=bool(data.get("sound_enabled", True)),
        wait_for_selector=str(
            data.get("wait_for_selector", ".leaflet-marker-icon.map-incident")
        ),
        dynamic_selector=str(
            data.get("dynamic_selector", ".leaflet-marker-icon.map-incident")
        ),
        coordinate_mode=str(data.get("coordinate_mode", "latlng")),
        map_coordinate_precision=int(data.get("map_coordinate_precision", 4)),
        map_container_selector=str(
            data.get("map_container_selector", ".leaflet-container")
        ),
        reference_zoom=float(data.get("reference_zoom", 0)),
        lock_scale=bool(data.get("lock_scale", False)),
        static_source=str(data.get("static_source", "config")),
        static_objects=objects,
    )


def validate_settings(settings: RuntimeSettings) -> RuntimeSettings:
    """Проверяет ограничения и возвращает нормализованную копию настроек."""
    site = settings.site.strip()
    if not site:
        raise ValueError("Укажите сайт для парсинга.")
    cdp_url = settings.cdp_url.strip()
    if not cdp_url:
        raise ValueError("Укажите адрес CDP браузера.")
    if settings.interval_seconds < 0.4:
        raise ValueError("Интервал парсинга должен быть не меньше 0.4 с.")
    if settings.threshold <= 0:
        raise ValueError("Порог красного выделения должен быть больше 0.")
    warning = max(settings.warning_distance, settings.threshold)
    blue = max(settings.blue_distance, warning)
    mode = normalize_mode(settings.coordinate_mode)
    if settings.map_coordinate_precision not in {3, 4, 5, 6}:
        raise ValueError("Точность координат на карте должна быть от 3 до 6 знаков.")
    for item in settings.static_objects:
        # x/y пересчитаются в lat/lng по текущему виду карты при опросе.
        if (
            mode == "latlng"
            and (item.lat is None or item.lng is None)
            and item.x == 0
            and item.y == 0
        ):
            raise ValueError(
                f"Точка '{item.name}': в режиме latlng укажите lat/lng "
                "(географические) либо x/y относительно карты."
            )
    source = (
        settings.static_source
        if settings.static_source in {"config", "page", "both"}
        else "config"
    )
    return RuntimeSettings(
        site=site,
        cdp_url=cdp_url,
        interval_seconds=float(settings.interval_seconds),
        threshold=float(settings.threshold),
        warning_distance=float(warning),
        blue_distance=float(blue),
        alert_color=_norm_color(settings.alert_color, "цвета красного выделения"),
        warning_color=_norm_color(settings.warning_color, "цвета жёлтого выделения"),
        blue_color=_norm_color(settings.blue_color, "цвета синего выделения"),
        sound_enabled=bool(settings.sound_enabled),
        wait_for_selector=settings.wait_for_selector.strip()
        or settings.dynamic_selector.strip(),
        dynamic_selector=settings.dynamic_selector.strip()
        or ".leaflet-marker-icon.map-incident",
        coordinate_mode=mode,
        map_coordinate_precision=settings.map_coordinate_precision,
        map_container_selector=settings.map_container_selector.strip()
        or ".leaflet-container",
        reference_zoom=float(settings.reference_zoom),
        lock_scale=bool(settings.lock_scale),
        static_source=source,
        static_objects=list(settings.static_objects),
    )


def _norm_color(value: str, label: str) -> str:
    """Проверяет цвет ``#RRGGBB`` и приводит его к нижнему регистру."""
    text = (value or "").strip()
    if not text.startswith("#") and len(text) == 6:
        text = f"#{text}"
    if not _HEX.match(text):
        raise ValueError(f"Некорректный {label}. Нужен формат #RRGGBB.")
    return text.lower()


def _optional(value: Any) -> float | None:
    """Преобразует необязательное поле координаты в число."""
    if value is None or value == "":
        return None
    return float(value)
