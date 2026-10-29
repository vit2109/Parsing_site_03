"""Оповещения MAX при входе динамического объекта в выбранную зону."""

from __future__ import annotations

import json
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
from typing import TYPE_CHECKING, Any

from parsing_site.capture_archive import ScreenshotRecord
from parsing_site.config import BUNDLE_ROOT, ROOT
from parsing_site.coordinates import MapView, latlng_to_container
from parsing_site.settings_store import RuntimeSettings, resolve_max_token
from parsing_site.tracking import DistanceRow, TrackingSnapshot

if TYPE_CHECKING:
    from parsing_site.db import Database

API = "https://platform-api2.max.ru"
SEVERITY = {"ok": 0, "blue": 1, "warning": 2, "alert": 3}
ZONE_NAME = {"blue": "синюю", "warning": "жёлтую", "alert": "красную"}
PHOTO_MISSING_NOTE = "Скриншот с номером и ближайшей стационарной точкой не найден."


@lru_cache(maxsize=1)
def _ssl_context() -> ssl.SSLContext:
    """Добавляет проверенный CA-набор к системным сертификатам."""
    context = ssl.create_default_context()
    for path in (
        ROOT / "certs" / "ca-bundle.pem",
        BUNDLE_ROOT / "certs" / "ca-bundle.pem",
        ROOT.parent / "MAX_BOT" / "certs" / "ca-bundle.pem",
        ROOT.parent.parent / "MAX_BOT" / "certs" / "ca-bundle.pem",
    ):
        if path.is_file():
            context.load_verify_locations(cafile=str(path))
            break
    return context


@dataclass(slots=True)
class MaxDelivery:
    user_id: int
    token: str
    text: str
    screenshot: Path | None
    test: bool = False
    log_id: int | None = None
    dynamic_index: int = 0
    nearest_static_index: int = 0
    nearest_static_name: str = ""
    nearest_static: tuple[float, float] | None = None
    related_statics: tuple[tuple[float, float], ...] = ()


class MaxAlertEngine:
    """Сравнивает зоны между опросами отдельно для каждого получателя."""

    def __init__(self) -> None:
        self._previous: dict[tuple[int, int, int], str] = {}
        self._day: str | None = None

    def collect(
        self, snapshot: TrackingSnapshot, settings: RuntimeSettings,
        screenshots: list[ScreenshotRecord],
    ) -> list[MaxDelivery]:
        """Возвращает сообщения только для входов во внутреннюю зону."""
        today = datetime.now().astimezone().date().isoformat()
        if today != self._day:
            self._previous.clear()
            self._day = today
        if not settings.max_enabled:
            self._previous.clear()
            return []
        rows = {(row.dynamic_index, row.static_index): row for row in snapshot.rows}
        dynamics = {item.index: item for item in snapshot.dynamics}
        zone_counts: dict[int, dict[str, int]] = {}
        for row in snapshot.rows:
            if row.status in {"alert", "warning", "blue"}:
                counts = zone_counts.setdefault(row.static_index, {
                    "alert": 0, "warning": 0, "blue": 0,
                })
                counts[row.status] += 1

        def static_label(row: DistanceRow) -> str:
            counts = zone_counts.get(row.static_index, {})
            return (
                f"{row.static_name} (красная: {counts.get('alert', 0)}, "
                f"жёлтая: {counts.get('warning', 0)}, "
                f"синяя: {counts.get('blue', 0)})"
            )

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
                selected = rules[static_id].zones
                if row.status not in selected or (
                    previous in selected and SEVERITY[row.status] <= SEVERITY[previous]
                ):
                    continue
                entered.setdefault(dynamic_id, []).append(row)
            for dynamic_id, triggered in entered.items():
                all_distances = sorted(
                    (row for (row_dynamic_id, _), row in rows.items()
                     if row_dynamic_id == dynamic_id),
                    key=lambda item: item.distance,
                )
                if not all_distances:
                    continue
                distances = [
                    row for row in all_distances
                    if row.distance_unit != "km"
                    or row.distance <= settings.max_display_distance_km
                ]
                unit = "км" if all_distances[0].distance_unit == "km" else all_distances[0].distance_unit
                label = f"№{dynamic_id}" if dynamic_id > 0 else "номер не определён"
                dynamic = dynamics.get(dynamic_id)
                object_name = dynamic.object_type if dynamic and dynamic.object_type else all_distances[0].dynamic_name
                entered_ids = {item.static_index for item in triggered}
                lines = [
                    f"Всего динамических объектов: {len(snapshot.dynamics)}.",
                    f"Объект {label} ({object_name}) вошёл в выбранные зоны.",
                ]
                if distances:
                    lines.extend([
                        (f"Ближайший объект: «{static_label(distances[0])}», "
                         f"{distances[0].distance:.1f} {unit}."),
                        "Расстояния до стационарных объектов (★ — новый вход):",
                        *(
                            f"{'★' if item.static_index in entered_ids else '•'} "
                            f"{static_label(item)}: {item.distance:.1f} {unit}"
                            + (f", вошёл в {ZONE_NAME[item.status]} зону"
                               if item.static_index in entered_ids else "")
                            for item in distances
                        ),
                    ])
                else:
                    lines.append(
                        "Стационарных объектов в пределах "
                        f"{settings.max_display_distance_km:g} км нет."
                    )
                nearest = distances[0] if distances else None
                static = self._geo(nearest) if nearest else None
                related = tuple(
                    point for row in distances if (point := self._geo(row)) is not None
                )
                photo = self.select_screenshot(
                    dynamic_id, static, related, screenshots,
                    datetime.now().astimezone(),
                )
                if photo is None and nearest is not None:
                    lines.append(PHOTO_MISSING_NOTE)
                deliveries.append(MaxDelivery(
                    recipient.user_id, resolve_max_token(settings), "\n".join(lines)[:4000], photo,
                    dynamic_index=dynamic_id,
                    nearest_static_index=nearest.static_index if nearest else 0,
                    nearest_static_name=nearest.static_name if nearest else "",
                    nearest_static=static,
                    related_statics=related,
                ))
        self._previous = {key: value for key, value in self._previous.items() if key in active}
        return deliveries

    @staticmethod
    def _geo(row: DistanceRow) -> tuple[float, float] | None:
        if row.static_lat is None or row.static_lng is None:
            return None
        return row.static_lat, row.static_lng

    @staticmethod
    def _visible(view: MapView | None, point: tuple[float, float]) -> bool:
        projected = latlng_to_container(*point, view) if view is not None else None
        return projected is not None and (
            24 <= projected[0] <= view.width - 24
            and 24 <= projected[1] <= view.height - 24
        )

    @classmethod
    def select_screenshot(
        cls, number: int, nearest: tuple[float, float] | None,
        related: tuple[tuple[float, float], ...],
        screenshots: list[ScreenshotRecord], at: datetime,
    ) -> Path | None:
        """Выбирает кадр текущего обхода с номером и ближайшей точкой."""
        if nearest is None:
            return None
        matches = [
            item for item in screenshots
            if item.path.is_file() and number in item.numbers
            and cls._visible(item.view, nearest)
        ]
        if not matches:
            return None
        best = max(matches, key=lambda item: (
            sum(cls._visible(item.view, point) for point in related),
            -abs((item.captured_at - at).total_seconds()),
        ))
        return best.path


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


def send_max_message(delivery: MaxDelivery) -> str | None:
    """Отправляет сообщение; при ошибке JPEG сохраняет доставку текста."""
    body: dict[str, Any] = {"text": delivery.text}
    photo_error: str | None = None
    if delivery.screenshot:
        try:
            if not delivery.screenshot.is_file():
                raise FileNotFoundError("Файл снимка недоступен")
            body["attachments"] = [{
                "type": "image",
                "payload": _upload_image(delivery.token, delivery.screenshot),
            }]
        except Exception as exc:  # noqa: BLE001 — текст события всё ещё нужно доставить.
            photo_error = str(exc)
            body["text"] += "\nСкриншот не удалось прикрепить."
    url = f"{API}/messages?user_id={delivery.user_id}"
    for attempt in range(5):
        try:
            _request_json(url, delivery.token, json.dumps(body, ensure_ascii=False).encode("utf-8"))
            return photo_error
        except RuntimeError as exc:
            if "attachments" not in body or "attachment.not.ready" not in str(exc):
                raise
            if attempt == 4:
                photo_error = str(exc)
                del body["attachments"]
                body["text"] += "\nСкриншот не удалось прикрепить."
                _request_json(url, delivery.token, json.dumps(body, ensure_ascii=False).encode("utf-8"))
                return photo_error
            time.sleep(2 ** attempt)
    return photo_error


class MaxDispatch:
    """Фоновая очередь, ограничивающая отправку одному пользователю."""

    def __init__(self, log: Callable[[str, str, str], None],
                 current_settings: Callable[[], RuntimeSettings],
                 database: Database | None = None) -> None:
        self._queue: queue.Queue[MaxDelivery] = queue.Queue()
        self._log = log
        self._current_settings = current_settings
        self._database = database
        self._last_sent: dict[int, float] = {}
        self._thread: threading.Thread | None = None

    def submit(self, delivery: MaxDelivery) -> int | None:
        if self._database is not None:
            delivery.log_id = self._database.add_max_delivery(
                delivery.user_id, "test" if delivery.test else "alert",
                delivery.text, str(delivery.screenshot) if delivery.screenshot else None,
            )
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="max-dispatch", daemon=True)
            self._thread.start()
        self._queue.put_nowait(delivery)
        pending = self._queue.qsize()
        if pending in {100, 500, 1000, 5000}:
            self._log("WARNING", "max", f"В очереди MAX {pending} уведомлений: доставка задержится")
        return delivery.log_id

    def submit_test(self, user_id: int) -> int | None:
        """Ставит в ту же очередь диагностическое текстовое сообщение."""
        token = resolve_max_token(self._current_settings())
        if not token:
            raise ValueError("Токен MAX не настроен")
        text = f"Тестовое сообщение Parsing Site. Время: {datetime.now().astimezone():%d.%m.%Y %H:%M:%S}."
        return self.submit(MaxDelivery(user_id, token, text, None, test=True))

    def _run(self) -> None:
        while True:
            delivery = self._queue.get()
            try:
                settings = self._current_settings()
                if not delivery.test and (not settings.max_enabled or not any(
                    recipient.user_id == delivery.user_id
                    for recipient in settings.max_recipients
                )):
                    if self._database is not None and delivery.log_id is not None:
                        self._database.finish_max_delivery(
                            delivery.log_id, "skipped", "Рассылка выключена или получатель удалён",
                        )
                    continue
                delivery.token = resolve_max_token(settings)
                if not delivery.token:
                    raise ValueError("Токен MAX не настроен")
                wait = 0.5 - (time.monotonic() - self._last_sent.get(delivery.user_id, 0))
                if wait > 0:
                    time.sleep(wait)
                photo_error = send_max_message(delivery)
                self._last_sent[delivery.user_id] = time.monotonic()
                if self._database is not None and delivery.log_id is not None:
                    self._database.finish_max_delivery(
                        delivery.log_id, "sent",
                        f"Скриншот не отправлен: {photo_error}"
                        if isinstance(photo_error, str) and photo_error else "",
                    )
                if isinstance(photo_error, str) and photo_error:
                    self._log("WARNING", "max", f"user_id={delivery.user_id}: скриншот не отправлен: {photo_error}")
                self._log("INFO", "max", f"Сообщение отправлено user_id={delivery.user_id}, запись #{delivery.log_id}")
            except Exception as exc:  # noqa: BLE001 — ошибка доставки не останавливает опрос.
                if self._database is not None and delivery.log_id is not None:
                    self._database.finish_max_delivery(delivery.log_id, "error", str(exc))
                self._log("WARNING", "max", f"user_id={delivery.user_id}, запись #{delivery.log_id}: {exc}")
            finally:
                self._queue.task_done()
