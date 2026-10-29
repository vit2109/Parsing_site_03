"""Настройки из SQLite (перекрывают config.toml, пока пользователь не сбросит БД)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from typing import Any

from parsing_site.config import ROOT, AppConfig, ScanZoneConfig, StaticObjectConfig, load_config
from parsing_site.coordinates import normalize_mode
from parsing_site.db import Database

_HEX = re.compile(r"^#([0-9a-fA-F]{6})$")
COORD_MODES = {"screen", "map_relative", "percent", "latlng"}
MAX_ZONES = {"blue", "warning", "alert"}


@lru_cache(maxsize=1)
def _max_token_from_dotenv() -> str:
    """Ищет токен в локальном .env и существующем соседнем проекте бота."""
    for path in (
        ROOT / ".env",
        ROOT.parent / "MAX_BOT" / ".env",
        ROOT.parent.parent / "MAX_BOT" / ".env",
    ):
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            key, separator, value = line.partition("=")
            if separator and key.strip() == "MAX_BOT_TOKEN":
                return value.strip().strip('"\'')
    return ""


def resolve_max_token(settings: RuntimeSettings) -> str:
    """Возвращает токен из настроек, окружения или .env без записи в БД."""
    return settings.max_token.strip() or os.environ.get("MAX_BOT_TOKEN", "").strip() \
        or _max_token_from_dotenv()


@dataclass(slots=True)
class MaxObjectRule:
    static_index: int
    zones: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MaxRecipient:
    user_id: int
    rules: list[MaxObjectRule] = field(default_factory=list)


@dataclass(slots=True)
class MaxRecipientGroup:
    user_ids: list[int] = field(default_factory=list)
    all_objects: bool = False
    zones: list[str] = field(default_factory=list)
    rules: list[MaxObjectRule] = field(default_factory=list)


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
    scan_wait_seconds: float = 1.5
    scan_max_zoom: int = 10
    scan_mode: str = "adaptive"
    scan_group_radius_km: float = 50.0
    scan_zones: list[ScanZoneConfig] = field(default_factory=list)
    screenshot_enabled: bool = False
    screenshot_dir: str = "data/screenshots"
    screenshot_period_hours: int = 1
    screenshot_width: int = 1280
    screenshot_height: int = 720
    archive_html_enabled: bool = False
    archive_html_dir: str = "data/html"
    archive_html_period_hours: int = 1
    max_enabled: bool = False
    max_token: str = ""
    max_display_distance_km: float = 150.0
    max_recipients: list[MaxRecipient] = field(default_factory=list)
    max_groups: list[MaxRecipientGroup] = field(default_factory=list)
    lock_scale: bool = False
    static_source: str = "config"
    static_objects: list[StaticObjectConfig] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Сериализует настройки и вложенные точки в JSON-совместимый словарь."""
        payload = asdict(self)
        payload["static_objects"] = [asdict(item) for item in self.static_objects]
        return payload

    def public_dict(self) -> dict[str, Any]:
        """Возвращает настройки для браузера без секретного токена."""
        payload = self.to_dict()
        payload["max_token"] = ""
        payload["max_token_set"] = bool(resolve_max_token(self))
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
                scan_wait_seconds=self.scan_wait_seconds,
                scan_max_zoom=self.scan_max_zoom,
                scan_mode=self.scan_mode,
                scan_group_radius_km=self.scan_group_radius_km,
                scan_zones=list(self.scan_zones),
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
        scan_wait_seconds=cfg.tracking.scan_wait_seconds,
        scan_max_zoom=cfg.tracking.scan_max_zoom,
        scan_mode=cfg.tracking.scan_mode,
        scan_group_radius_km=cfg.tracking.scan_group_radius_km,
        scan_zones=list(cfg.tracking.scan_zones),
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
    scan_zones = [
        ScanZoneConfig(
            name=str(item.get("name") or f"Сектор {index}"),
            lat=float(item["lat"]),
            lng=float(item["lng"]),
            radius_km=float(item.get("radius_km", 350)),
        )
        for index, item in enumerate(data.get("scan_zones") or [], start=1)
    ]
    recipients = [
        MaxRecipient(
            user_id=int(item.get("user_id", 0)),
            rules=[MaxObjectRule(
                static_index=int(rule.get("static_index", 0)),
                zones=[str(zone) for zone in rule.get("zones") or []],
            ) for rule in item.get("rules") or []],
        ) for item in data.get("max_recipients") or []
    ]
    raw_groups = data.get("max_groups")
    if raw_groups is None:
        raw_groups = [
            {"user_ids": [recipient.user_id], "rules": [asdict(rule) for rule in recipient.rules]}
            for recipient in recipients
        ]
    groups = []
    for item in raw_groups:
        raw_ids = item.get("user_ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [part for part in re.split(r"[\s,;]+", raw_ids.strip()) if part]
        groups.append(MaxRecipientGroup(
            user_ids=[int(user_id) for user_id in raw_ids],
            all_objects=bool(item.get("all_objects", False)),
            zones=[str(zone) for zone in item.get("zones") or []],
            rules=[MaxObjectRule(
                static_index=int(rule.get("static_index", 0)),
                zones=[str(zone) for zone in rule.get("zones") or []],
            ) for rule in item.get("rules") or []],
        ))
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
        scan_wait_seconds=float(data.get("scan_wait_seconds", 1.5)),
        scan_max_zoom=int(data.get("scan_max_zoom", 10)),
        scan_mode=str(data.get("scan_mode", "adaptive")),
        scan_group_radius_km=float(data.get("scan_group_radius_km", 50)),
        scan_zones=scan_zones,
        screenshot_enabled=bool(data.get("screenshot_enabled", False)),
        screenshot_dir=str(data.get("screenshot_dir", "data/screenshots")),
        screenshot_period_hours=int(data.get("screenshot_period_hours", 1)),
        screenshot_width=int(data.get("screenshot_width", 1280)),
        screenshot_height=int(data.get("screenshot_height", 720)),
        archive_html_enabled=bool(data.get("archive_html_enabled", False)),
        archive_html_dir=str(data.get("archive_html_dir", "data/html")),
        archive_html_period_hours=int(data.get("archive_html_period_hours", 1)),
        max_enabled=bool(data.get("max_enabled", False)),
        max_token=str(data.get("max_token", "")),
        max_display_distance_km=float(data.get("max_display_distance_km", 150)),
        max_recipients=recipients,
        max_groups=groups,
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
    if not 0.2 <= settings.scan_wait_seconds <= 10:
        raise ValueError("Задержка на точке должна быть от 0.2 до 10 с.")
    if not 4 <= settings.scan_max_zoom <= 18:
        raise ValueError("Максимальный zoom обхода должен быть от 4 до 18.")
    if settings.scan_mode not in {"adaptive", "static_points", "grouped_static", "zones"}:
        raise ValueError("Неизвестный режим обхода карты.")
    if not 1 <= settings.scan_group_radius_km <= 500:
        raise ValueError("Радиус объединения стационарных точек должен быть от 1 до 500 км.")
    if settings.scan_mode in {"static_points", "grouped_static"} \
            and settings.coordinate_mode != "latlng":
        raise ValueError("Обход по стационарным точкам требует режима координат lat/lng.")
    if settings.scan_mode in {"static_points", "grouped_static"} and not any(
        point.lat is not None and point.lng is not None
        and -85 <= point.lat <= 85 and -180 <= point.lng <= 180
        for point in settings.static_objects
    ):
        raise ValueError("Для обхода по стационарным точкам укажите их lat/lng.")
    if settings.scan_mode == "zones" and not settings.scan_zones:
        raise ValueError("Для обхода по секторам добавьте хотя бы один сектор.")
    if len(settings.scan_zones) > 24:
        raise ValueError("Допускается не более 24 секторов обхода.")
    for zone in settings.scan_zones:
        if not zone.name.strip() or not -85 <= zone.lat <= 85 \
                or not -180 <= zone.lng <= 180 or not 1 <= zone.radius_km <= 2000:
            raise ValueError("Сектор обхода: укажите название, lat/lng и радиус 1–2000 км.")
    if settings.screenshot_period_hours not in {1, 3, 6, 12, 24} \
            or settings.archive_html_period_hours not in {1, 3, 6, 12, 24}:
        raise ValueError("Период сохранения должен быть 1, 3, 6, 12 или 24 часа.")
    if not 320 <= settings.screenshot_width <= 3840 \
            or not 240 <= settings.screenshot_height <= 2160:
        raise ValueError("Размер снимка: ширина 320–3840, высота 240–2160 пикселей.")
    if settings.screenshot_enabled and not settings.screenshot_dir.strip():
        raise ValueError("Укажите каталог для снимков карты.")
    if settings.archive_html_enabled and not settings.archive_html_dir.strip():
        raise ValueError("Укажите каталог для HTML-страниц.")
    if not 0 < settings.max_display_distance_km <= 20_000:
        raise ValueError("Диапазон объектов в сообщении MAX должен быть от 0 до 20 000 км.")
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
    groups: list[MaxRecipientGroup] = []
    recipient_rules: dict[int, dict[int, list[str]]] = {}
    static_ids = {item.id for item in settings.static_objects}
    for group in settings.max_groups:
        user_ids = list(group.user_ids)
        if not user_ids or any(user_id <= 0 for user_id in user_ids) \
                or len(set(user_ids)) != len(user_ids):
            raise ValueError("В группе MAX укажите уникальные положительные user ID через пробел или запятую.")
        rules: list[MaxObjectRule] = []
        seen_statics: set[int] = set()
        if group.all_objects:
            zones = list(dict.fromkeys(group.zones))
            if not zones or any(zone not in MAX_ZONES for zone in zones):
                raise ValueError("Для всех объектов MAX выберите хотя бы одну зону.")
            rules = [MaxObjectRule(static_id, zones.copy()) for static_id in sorted(static_ids)]
        else:
            zones = []
            for rule in group.rules:
                if rule.static_index not in static_ids or rule.static_index in seen_statics:
                    raise ValueError("Объект MAX должен существовать и встречаться в группе только один раз.")
                rule_zones = list(dict.fromkeys(rule.zones))
                if not rule_zones or any(zone not in MAX_ZONES for zone in rule_zones):
                    raise ValueError("Для объекта MAX выберите синюю, жёлтую или красную зону.")
                seen_statics.add(rule.static_index)
                rules.append(MaxObjectRule(rule.static_index, rule_zones))
        if not rules:
            raise ValueError("Для группы MAX выберите объект и зону.")
        groups.append(MaxRecipientGroup(user_ids, group.all_objects, zones,
                                        [] if group.all_objects else rules))
        for user_id in user_ids:
            combined = recipient_rules.setdefault(user_id, {})
            for rule in rules:
                current = combined.setdefault(rule.static_index, [])
                current.extend(zone for zone in rule.zones if zone not in current)
    recipients = [
        MaxRecipient(user_id, [MaxObjectRule(static_id, zones)
                               for static_id, zones in sorted(rules.items())])
        for user_id, rules in recipient_rules.items()
    ]
    if settings.max_enabled and (not resolve_max_token(settings) or not recipients):
        raise ValueError("Для рассылки MAX укажите токен бота и хотя бы одного получателя.")
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
        scan_wait_seconds=float(settings.scan_wait_seconds),
        scan_max_zoom=int(settings.scan_max_zoom),
        scan_mode=settings.scan_mode,
        scan_group_radius_km=float(settings.scan_group_radius_km),
        scan_zones=[ScanZoneConfig(zone.name.strip(), zone.lat, zone.lng, zone.radius_km)
                    for zone in settings.scan_zones],
        screenshot_enabled=bool(settings.screenshot_enabled),
        screenshot_dir=settings.screenshot_dir.strip(),
        screenshot_period_hours=settings.screenshot_period_hours,
        screenshot_width=settings.screenshot_width,
        screenshot_height=settings.screenshot_height,
        archive_html_enabled=bool(settings.archive_html_enabled),
        archive_html_dir=settings.archive_html_dir.strip(),
        archive_html_period_hours=settings.archive_html_period_hours,
        max_enabled=bool(settings.max_enabled),
        max_token=settings.max_token.strip(),
        max_display_distance_km=float(settings.max_display_distance_km),
        max_recipients=recipients,
        max_groups=groups,
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
