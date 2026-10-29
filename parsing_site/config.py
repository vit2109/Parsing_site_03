"""
Загрузка config.toml, пути приложения и типизированные настройки.

config.toml лежит в корне проекта (или рядом с exe). Секция [tracking]
задаёт селекторы карты, пороги красный/жёлтый и статичные точки.
"""

from __future__ import annotations

import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def is_frozen() -> bool:
    """Возвращает ``True`` для приложения, собранного PyInstaller."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Каталог exe (или корень проекта) — для data/, config.toml, БД."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def bundle_root() -> Path:
    """Каталог встроенных ресурсов (web, шаблон config.toml)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return app_root()


ROOT = app_root()
BUNDLE_ROOT = bundle_root()
WEB_DIR = BUNDLE_ROOT / "web"
DEFAULT_CONFIG_PATH = ROOT / "config.toml"


def ensure_runtime_files() -> None:
    """При первом запуске exe копирует config.toml и start_chrome.ps1 рядом с exe."""
    if not is_frozen():
        return
    for name in ("config.toml", "start_chrome.ps1", "start_chrome.bat"):
        target = ROOT / name
        if target.exists():
            continue
        source = BUNDLE_ROOT / name
        if source.exists():
            shutil.copy2(source, target)


@dataclass(slots=True)
class BrowserConfig:
    """Параметры подключения к вкладке браузера по CDP."""

    cdp_url: str = "http://127.0.0.1:9222"
    url_contains: str = ""
    wait_for_selector: str = ""
    wait_timeout_ms: int = 10_000


@dataclass(slots=True)
class ScheduleConfig:
    """Частота фонового опроса страницы."""

    interval_seconds: float = 40.0


@dataclass(slots=True)
class ExtractConfig:
    """Правила универсального извлечения полей и HTML-таблиц."""

    extract_tables: bool = True
    tables_limit: int = 10
    selectors: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class OutputConfig:
    """Каталог и форматы файлов универсального HTML-парсера."""

    data_dir: Path = field(default_factory=lambda: ROOT / "data")
    save_jsonl: bool = True
    save_csv: bool = True
    save_html: bool = False


@dataclass(slots=True)
class StaticObjectConfig:
    """Статичная точка: id, имя, пиксели x/y и/или географические lat/lng."""

    id: int
    name: str
    x: float
    y: float
    lat: float | None = None
    lng: float | None = None


@dataclass(slots=True)
class TrackingConfig:
    """Правила съёма карты: селекторы, режим координат, пороги, статичные точки."""

    enabled: bool = True
    dynamic_selector: str = ".dynamic"
    static_selector: str = ".static"
    static_source: str = "config"
    coordinate_mode: str = "screen"
    map_container_selector: str = ".leaflet-container"
    reference_zoom: float = 0.0
    scan_wait_seconds: float = 1.5
    scan_max_zoom: int = 10
    lock_scale: bool = False
    threshold: float = 140.0
    warning_ratio: float = 1.4
    static_objects: list[StaticObjectConfig] = field(default_factory=list)
    warning_distance: float = 0.0
    blue_distance: float = 350.0
    alert_color: str = "#9b1c1c"
    warning_color: str = "#d4a017"
    blue_color: str = "#2878b8"

    @property
    def warning_limit(self) -> float:
        """Возвращает жёлтый порог, гарантированно не меньший красного."""
        if self.warning_distance > self.threshold:
            return self.warning_distance
        return self.threshold * max(1.0, self.warning_ratio)

    @property
    def blue_limit(self) -> float:
        """Возвращает синий порог, гарантированно не меньший жёлтого."""
        return max(self.blue_distance, self.warning_limit)


@dataclass(slots=True)
class AppConfig:
    """Совокупность всех настроек, загруженных из ``config.toml``."""

    browser: BrowserConfig = field(default_factory=BrowserConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    extract: ExtractConfig = field(default_factory=ExtractConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    path: Path = DEFAULT_CONFIG_PATH


def load_config(path: Path | None = None) -> AppConfig:
    """Читает TOML, применяет значения по умолчанию и готовит каталог данных."""
    ensure_runtime_files()
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        bundled = BUNDLE_ROOT / "config.toml"
        if bundled.exists() and bundled != config_path:
            config_path = bundled
        else:
            cfg = AppConfig(path=ROOT / "config.toml")
            cfg.output.data_dir.mkdir(parents=True, exist_ok=True)
            return cfg

    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    browser_raw = raw.get("browser", {})
    schedule_raw = raw.get("schedule", {})
    extract_raw = raw.get("extract", {})
    output_raw = raw.get("output", {})
    tracking_raw = raw.get("tracking", {})

    data_dir = Path(output_raw.get("data_dir", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir

    static_objects: list[StaticObjectConfig] = []
    for index, item in enumerate(tracking_raw.get("static_objects") or [], start=1):
        static_objects.append(
            StaticObjectConfig(
                id=int(item.get("id", index)),
                name=str(item.get("name", f"Статичный {index}")),
                x=float(item.get("x", 0)),
                y=float(item.get("y", 0)),
                lat=_optional_float(item.get("lat")),
                lng=_optional_float(item.get("lng")),
            )
        )

    source = str(tracking_raw.get("static_source", "config")).strip().lower()
    if source not in {"config", "page", "both"}:
        source = "config"

    cfg = AppConfig(
        browser=BrowserConfig(
            cdp_url=str(browser_raw.get("cdp_url", "http://127.0.0.1:9222")),
            url_contains=str(browser_raw.get("url_contains", "")),
            wait_for_selector=str(browser_raw.get("wait_for_selector", "")),
            wait_timeout_ms=int(browser_raw.get("wait_timeout_ms", 10_000)),
        ),
        schedule=ScheduleConfig(
            interval_seconds=float(schedule_raw.get("interval_seconds", 40)),
        ),
        extract=ExtractConfig(
            extract_tables=bool(extract_raw.get("extract_tables", True)),
            tables_limit=int(extract_raw.get("tables_limit", 10)),
            selectors=dict(extract_raw.get("selectors", {}) or {}),
        ),
        output=OutputConfig(
            data_dir=data_dir,
            save_jsonl=bool(output_raw.get("save_jsonl", True)),
            save_csv=bool(output_raw.get("save_csv", True)),
            save_html=bool(output_raw.get("save_html", False)),
        ),
        tracking=TrackingConfig(
            enabled=bool(tracking_raw.get("enabled", True)),
            dynamic_selector=str(
                tracking_raw.get(
                    "dynamic_selector", ".leaflet-marker-icon.map-incident"
                )
            ),
            static_selector=str(tracking_raw.get("static_selector", ".static")),
            static_source=source,
            coordinate_mode=str(tracking_raw.get("coordinate_mode", "latlng")),
            map_container_selector=str(
                tracking_raw.get("map_container_selector", ".leaflet-container")
            ),
            reference_zoom=float(tracking_raw.get("reference_zoom", 0.0)),
            scan_wait_seconds=float(tracking_raw.get("scan_wait_seconds", 1.5)),
            scan_max_zoom=int(tracking_raw.get("scan_max_zoom", 10)),
            lock_scale=bool(tracking_raw.get("lock_scale", False)),
            threshold=float(tracking_raw.get("threshold", 140.0)),
            warning_ratio=float(tracking_raw.get("warning_ratio", 1.4)),
            warning_distance=float(tracking_raw.get("warning_distance", 0.0)),
            blue_distance=float(tracking_raw.get("blue_distance", 350.0)),
            alert_color=str(tracking_raw.get("alert_color", "#9b1c1c")),
            warning_color=str(tracking_raw.get("warning_color", "#d4a017")),
            blue_color=str(tracking_raw.get("blue_color", "#2878b8")),
            static_objects=static_objects,
        ),
        path=config_path,
    )
    cfg.output.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _optional_float(value: object) -> float | None:
    """Преобразует необязательное значение TOML в число."""
    if value is None or value == "":
        return None
    return float(value)
