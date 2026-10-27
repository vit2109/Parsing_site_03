"""Оповещения MAX при входе динамического объекта в выбранную зону."""

from __future__ import annotations

import json
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from parsing_site.config import ROOT
from parsing_site.settings_store import RuntimeSettings, resolve_max_token
from parsing_site.tracking import DistanceRow, TrackingSnapshot

API = "https://platform-api2.max.ru"
SEVERITY = {"ok": 0, "blue": 1, "warning": 2, "alert": 3}
ZONE_NAME = {"blue": "синюю", "warning": "жёлтую", "alert": "красную"}


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Проверяет TLS с выбранным CA-набором; никогда не отключает проверку."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return ssl.create_default_context()
    for path in (
        ROOT / "certs" / "ca-bundle.pem",
        ROOT.parent / "MAX_BOT" / "certs" / "ca-bundle.pem",
        ROOT.parent.parent / "MAX_BOT" / "certs" / "ca-bundle.pem",
    ):
        if path.is_file():
            return ssl.create_default_context(cafile=str(path))
    return ssl.create_default_context()


@dataclass(slots=True)
class MaxDelivery:
    user_id: int
    token: str
    text: str
    screenshot: Path | None


class MaxAlertEngine:
    """Сравнивает зоны между опросами отдельно для каждого получателя."""

    def __init__(self) -> None:
        self._previous: dict[tuple[int, int, int], str] = {}
        self._day: str | None = None
        self._screenshots: list[tuple[Path, datetime, tuple[int, ...]]] = []

    def collect(
        self, snapshot: TrackingSnapshot, settings: RuntimeSettings,
        screenshots: list[tuple[Path, datetime, tuple[int, ...]]],
    ) -> list[MaxDelivery]:
        """Возвращает сообщения только для входов во внутреннюю зону."""
        today = datetime.now().astimezone().date().isoformat()
        if today != self._day:
            self._previous.clear()
            self._screenshots.clear()
            self._day = today
        self._screenshots.extend(screenshots)
        self._screenshots = [item for item in self._screenshots[-5000:] if item[0].is_file()]
        if not settings.max_enabled:
            self._previous.clear()
            return []
        rows = {(row.dynamic_index, row.static_index): row for row in snapshot.rows}
        dynamics = {item.index: item for item in snapshot.dynamics}
        active: set[tuple[int, int, int]] = set()
        deliveries: list[MaxDelivery] = []
        for recipient in settings.max_recipients:
            rules = {rule.static_index: rule for rule in recipient.rules}
            entered: dict[int, list[DistanceRow]] = {}
            for (dynamic_id, static_id), row in rows.items():
                if static_id not in rules:
                    continue
                key = (recipient.user_id, dynamic_id, static_id)
                active.add(key)
                previous = self._previous.get(key, "ok")
                self._previous[key] = row.status
                if SEVERITY[row.status] <= SEVERITY[previous] or row.status not in rules[static_id].zones:
                    continue
                entered.setdefault(dynamic_id, []).append(row)
            for dynamic_id, triggered in entered.items():
                distances = sorted(
                    (rows[(dynamic_id, rule_id)] for rule_id in rules
                     if (dynamic_id, rule_id) in rows),
                    key=lambda item: item.distance,
                )
                if not distances:
                    continue
                unit = "км" if distances[0].distance_unit == "km" else distances[0].distance_unit
                label = f"№{dynamic_id}" if dynamic_id > 0 else "номер не определён"
                dynamic = dynamics.get(dynamic_id)
                object_name = dynamic.object_type if dynamic and dynamic.object_type else distances[0].dynamic_name
                entered_ids = {item.static_index for item in triggered}
                lines = [
                    f"Объект {label} ({object_name}) вошёл в выбранные зоны.",
                    (f"Ближайший объект: «{distances[0].static_name}», "
                     f"{distances[0].distance:.1f} {unit}."),
                    "Расстояния до наблюдаемых объектов (★ — новый вход):",
                    *(
                        f"{'★' if item.static_index in entered_ids else '•'} "
                        f"{item.static_name}: {item.distance:.1f} {unit}"
                        + (f", вошёл в {ZONE_NAME[item.status]} зону"
                           if item.static_index in entered_ids else "")
                        for item in distances
                    ),
                ]
                photo = self._nearest_screenshot(dynamic_id, datetime.now().astimezone())
                if photo is None:
                    lines.append("Скриншот с видимым номером объекта не найден.")
                deliveries.append(MaxDelivery(
                    recipient.user_id, resolve_max_token(settings), "\n".join(lines)[:4000], photo,
                ))
        self._previous = {key: value for key, value in self._previous.items() if key in active}
        return deliveries

    def _nearest_screenshot(self, number: int, at: datetime) -> Path | None:
        matches = [item for item in self._screenshots if number in item[2]]
        return min(matches, key=lambda item: abs((item[1] - at).total_seconds()))[0] \
            if matches else None


def _request_json(url: str, token: str, body: bytes | None = None,
                  content_type: str = "application/json") -> dict[str, Any]:
    headers = {"Authorization": token, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body if body is not None else b"",
                                     headers=headers, method="POST")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=15, context=_ssl_context()) as response:
                content = response.read()
            return json.loads(content) if content else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read(1024).decode("utf-8", errors="replace")
            if exc.code in {429, 502, 503, 504} and attempt < 4:
                try:
                    delay = float(exc.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    delay = 2 ** attempt
                time.sleep(min(30.0, max(0.5, delay)))
                continue
            raise RuntimeError(f"MAX HTTP {exc.code}: {detail}") from exc
    raise RuntimeError("MAX: не удалось выполнить запрос после повторных попыток")


def _upload_image(token: str, path: Path) -> dict[str, Any]:
    slot = _request_json(f"{API}/uploads?type=image", token)
    upload_url = str(slot.get("url") or "")
    parsed = urllib.parse.urlparse(upload_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise RuntimeError("MAX вернул некорректный адрес загрузки изображения")
    boundary = f"max{time.time_ns()}"
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"data\"; "
            f"filename=\"{path.name}\"\r\nContent-Type: image/jpeg\r\n\r\n").encode() \
        + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    uploaded = _request_json(upload_url, token, body,
                             f"multipart/form-data; boundary={boundary}")
    photos = uploaded.get("photos") or {}
    if isinstance(photos, dict):
        for photo in photos.values():
            if isinstance(photo, dict) and photo.get("token"):
                return {"token": photo["token"]}
    media_token = uploaded.get("token") or slot.get("token")
    if media_token:
        return {"token": media_token}
    raise RuntimeError("MAX не вернул токен загруженного изображения")


def send_max_message(delivery: MaxDelivery) -> None:
    """Загружает JPEG и отправляет текст с изображением одним сообщением."""
    body: dict[str, Any] = {"text": delivery.text}
    if delivery.screenshot and delivery.screenshot.is_file():
        body["attachments"] = [{
            "type": "image",
            "payload": _upload_image(delivery.token, delivery.screenshot),
        }]
    url = f"{API}/messages?user_id={delivery.user_id}"
    for attempt in range(3):
        try:
            _request_json(url, delivery.token, json.dumps(body, ensure_ascii=False).encode("utf-8"))
            return
        except RuntimeError as exc:
            if "attachment.not.ready" not in str(exc) or attempt == 2:
                raise
            time.sleep(0.5 * (attempt + 1))


class MaxDispatch:
    """Фоновая очередь, ограничивающая отправку одному пользователю."""

    def __init__(self, log: Callable[[str, str, str], None],
                 current_settings: Callable[[], RuntimeSettings]) -> None:
        self._queue: queue.Queue[MaxDelivery] = queue.Queue()
        self._log = log
        self._current_settings = current_settings
        self._last_sent: dict[int, float] = {}
        self._thread: threading.Thread | None = None

    def submit(self, delivery: MaxDelivery) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="max-dispatch", daemon=True)
            self._thread.start()
        self._queue.put_nowait(delivery)
        pending = self._queue.qsize()
        if pending in {100, 500, 1000, 5000}:
            self._log("WARNING", "max", f"В очереди MAX {pending} уведомлений: доставка задержится")

    def _run(self) -> None:
        while True:
            delivery = self._queue.get()
            try:
                settings = self._current_settings()
                if not settings.max_enabled or not any(
                    recipient.user_id == delivery.user_id
                    for recipient in settings.max_recipients
                ):
                    continue
                delivery.token = resolve_max_token(settings)
                wait = 0.5 - (time.monotonic() - self._last_sent.get(delivery.user_id, 0))
                if wait > 0:
                    time.sleep(wait)
                send_max_message(delivery)
                self._last_sent[delivery.user_id] = time.monotonic()
                self._log("INFO", "max", f"Сообщение отправлено user_id={delivery.user_id}")
            except Exception as exc:  # noqa: BLE001 — ошибка доставки не останавливает опрос.
                self._log("WARNING", "max", f"user_id={delivery.user_id}: {exc}")
            finally:
                self._queue.task_done()
