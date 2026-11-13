"""Локальный ключ управления для любого способа запуска веб-приложения."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from parsing_site.config import ROOT


def register(port: int, cdp_url: str) -> tuple[Path, str]:
    """Публикует ключ только в локальном файле рядом с данными программы."""
    token = os.environ.get("PARSING_SITE_LAUNCHER_TOKEN") or secrets.token_urlsafe(32)
    path = ROOT / "data" / f"launcher-{port}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"application": "parsing-site", "version": 1, "web_port": port,
               "cdp_url": cdp_url, "instance": os.environ.get("PARSING_SITE_INSTANCE") or None,
               "pid": os.getpid(), "token": token}
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path, token


def unregister(path: Path, token: str) -> None:
    """Не удаляет запись, если порт уже перешёл другому процессу."""
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("token") == token:
            path.unlink(missing_ok=True)
    except (OSError, ValueError, AttributeError):
        pass
