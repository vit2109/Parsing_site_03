"""Архив каждого обхода карты с номерами, разложенный по периодам."""

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


def capture_due(session: Any, settings: RuntimeSettings) -> tuple[list[Path], list[str]]:
    """Сохраняет каждый включённый архив из подписанного вида текущего обхода."""
    capture = session.take_numbered_capture()
    if capture is None:
        errors = []
        if settings.screenshot_enabled and session.numbered_capture_error:
            errors.append(f"map: {session.numbered_capture_error}")
        if settings.archive_html_enabled and session.numbered_html_error:
            errors.append(f"page: {session.numbered_html_error}")
        return [], errors
    timestamp = capture.captured_at.strftime("%Y-%m-%d_%H-%M-%S-%f")
    zoom_label = f"{capture.zoom:g}".replace(".", "_")
    saved: list[Path] = []
    errors: list[str] = []
    jobs = (
        (settings.screenshot_enabled, settings.screenshot_dir,
         settings.screenshot_period_hours, "jpg", "map", capture.image,
         session.numbered_capture_error),
        (settings.archive_html_enabled, settings.archive_html_dir,
         settings.archive_html_period_hours, "html", "page", capture.html,
         session.numbered_html_error),
    )
    for enabled, directory, hours, suffix, prefix, content, capture_error in jobs:
        if not enabled:
            continue
        if content is None:
            if capture_error:
                errors.append(f"{prefix}: {capture_error}")
            continue
        folder = period_path(directory, hours, capture.captured_at)
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
                saved.append(target)
            finally:
                temporary.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 — архив не должен прерывать опрос.
            errors.append(f"{prefix}: {exc}")
    return saved, errors
