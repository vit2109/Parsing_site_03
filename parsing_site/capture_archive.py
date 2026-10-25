"""Периодическое сохранение вида карты и HTML после завершённого опроса."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from parsing_site.config import ROOT
from parsing_site.settings_store import RuntimeSettings


def period_path(directory: str, hours: int, captured_at: datetime) -> Path:
    """Папка календарного периода по местному времени компьютера."""
    root = Path(directory).expanduser()
    if not root.is_absolute():
        root = ROOT / root
    start_hour = captured_at.hour // hours * hours
    return root / captured_at.strftime("%Y-%m-%d") / f"{start_hour:02d}-{start_hour + hours:02d}"


def capture_due(
    session: Any,
    settings: RuntimeSettings,
    zoom: float | None,
    *,
    captured_at: datetime | None = None,
) -> tuple[list[Path], list[str]]:
    """Сохраняет по одному файлу каждого включённого типа за свой период."""
    now = captured_at or datetime.now().astimezone()
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    zoom_label = f"{zoom:g}".replace(".", "_") if zoom is not None else "unknown"
    saved: list[Path] = []
    errors: list[str] = []
    jobs = (
        (settings.screenshot_enabled, settings.screenshot_dir,
         settings.screenshot_period_hours, "jpg", "map"),
        (settings.archive_html_enabled, settings.archive_html_dir,
         settings.archive_html_period_hours, "html", "page"),
    )
    for enabled, directory, hours, suffix, prefix in jobs:
        if not enabled:
            continue
        folder = period_path(directory, hours, now)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if next(folder.glob(f"{prefix}_*.{suffix}"), None) is not None:
                continue
            target = folder / f"{prefix}_{timestamp}_zoom-{zoom_label}.{suffix}"
            temporary = target.with_suffix(target.suffix + ".tmp")
            try:
                if suffix == "jpg":
                    content = session.capture_map_image(
                        settings.map_container_selector,
                        settings.screenshot_width,
                        settings.screenshot_height,
                    )
                    temporary.write_bytes(content)
                else:
                    temporary.write_text(session.capture_html(), encoding="utf-8")
                temporary.replace(target)
                saved.append(target)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 — архив не должен прерывать опрос.
            errors.append(f"{prefix}: {exc}")
    return saved, errors
