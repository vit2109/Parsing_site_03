"""Архив каждого обхода карты с номерами, разложенный по периодам."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from parsing_site.config import ROOT
from parsing_site.coordinates import MapView
from parsing_site.settings_store import RuntimeSettings


def period_path(directory: str, hours: int, captured_at: datetime) -> Path:
    """Папка календарного периода по местному времени компьютера."""
    root = Path(directory).expanduser()
    if not root.is_absolute():
        root = ROOT / root
    start_hour = captured_at.hour // hours * hours
    return root / captured_at.strftime("%Y-%m-%d") / f"{start_hour:02d}-{start_hour + hours:02d}"


@dataclass(slots=True)
class ScreenshotRecord:
    """Файл кадра вместе с видимыми номерами и геометрией карты."""

    path: Path
    captured_at: datetime
    numbers: tuple[int, ...]
    view: MapView | None


class CaptureArchive:
    """Сохраняет каждый подписанный вид сразу, не накапливая JPEG в памяти."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.saved: list[Path] = []
        self.errors: list[str] = []
        self.screenshots: list[ScreenshotRecord] = []

    def save(self, capture: Any) -> None:
        """Пишет JPEG вида и первый HTML обхода в выбранные папки."""
        jobs = (
            (self.settings.screenshot_enabled or self.settings.max_enabled,
             self.settings.screenshot_dir if self.settings.screenshot_enabled
             else str(ROOT / "data" / "max_bot_screenshots"),
             self.settings.screenshot_period_hours, "jpg", "map", capture.image),
            (self.settings.archive_html_enabled, self.settings.archive_html_dir,
             self.settings.archive_html_period_hours, "html", "page", capture.html),
        )
        for enabled, directory, hours, suffix, prefix, content in jobs:
            if enabled and content is not None:
                path = self._write(directory, hours, suffix, prefix, content,
                                   capture.captured_at, capture.zoom)
                if path is not None and suffix == "jpg":
                    self.screenshots.append(ScreenshotRecord(
                        path, capture.captured_at, capture.numbers, capture.view,
                    ))

    def _write(
        self, directory: str, hours: int, suffix: str, prefix: str,
        content: bytes | str, captured_at: datetime, zoom: float,
    ) -> Path | None:
        timestamp = captured_at.strftime("%Y-%m-%d_%H-%M-%S-%f")
        zoom_label = f"{zoom:g}".replace(".", "_")
        folder = period_path(directory, hours, captured_at)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            stem = f"{prefix}_{timestamp}_zoom-{zoom_label}"
            target = folder / f"{stem}.{suffix}"
            copy = 2
            while target.exists():
                target = folder / f"{stem}_{copy}.{suffix}"
                copy += 1
            temporary = target.with_suffix(target.suffix + ".tmp")
            try:
                if isinstance(content, bytes):
                    temporary.write_bytes(content)
                else:
                    temporary.write_text(content, encoding="utf-8")
                temporary.replace(target)
                self.saved.append(target)
                return target
            finally:
                temporary.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 — архив не должен прерывать опрос.
            self.errors.append(f"{prefix}: {exc}")
        return None

    def finish(self, session: Any) -> tuple[list[Path], list[str]]:
        """Добавляет ошибки съёмки к ошибкам записи и возвращает результат."""
        errors = self.errors.copy()
        if (self.settings.screenshot_enabled or self.settings.max_enabled) and session.numbered_capture_error:
            errors.append(f"map: {session.numbered_capture_error}")
        if self.settings.archive_html_enabled and session.numbered_html_error:
            errors.append(f"page: {session.numbered_html_error}")
        return self.saved, errors


def capture_due(session: Any, settings: RuntimeSettings) -> tuple[list[Path], list[str]]:
    """Сохраняет архивы из сеанса без потоковой записи (для ручных вызовов)."""
    archive = CaptureArchive(settings)
    while (capture := session.take_numbered_capture()) is not None:
        archive.save(capture)
    return archive.finish(session)
