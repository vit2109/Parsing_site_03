"""Фоновый опрос вкладки Chrome и загрузка тестового HTML в таблицу UI."""

from __future__ import annotations

import threading
from datetime import datetime
from time import monotonic
from typing import Any

from parsing_site.browser import BrowserNotConnectedError, BrowserSession
from parsing_site.capture_archive import CaptureArchive
from parsing_site.config import load_config
from parsing_site.db import Database
from parsing_site.max_bot import PHOTO_MISSING_NOTE, MaxAlertEngine, MaxDispatch
from parsing_site.settings_store import SettingsStore, resolve_max_token
from parsing_site.table_history_db import TableHistoryDatabase
from parsing_site.tracking import capture_snapshot, snapshot_to_dict


class ParseWorker:
    """Управляет единственным фоновым потоком опроса и его общим состоянием."""

    def __init__(
        self,
        db: Database,
        store: SettingsStore,
        table_history: TableHistoryDatabase,
    ) -> None:
        """Связывает рабочий поток с настройками и двумя хранилищами истории."""
        self._db = db
        self._store = store
        self._table_history = table_history
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._latest: dict[str, Any] | None = None
        self._status = "stopped"
        self._error = ""
        self._polls = 0
        self._max_poll: dict[str, Any] | None = None
        self._max_dispatch = MaxDispatch(db.add_log, store.current, db)

    def send_max_test(self, user_id: int) -> int | None:
        """Ставит тестовое сообщение MAX указанному пользователю в очередь."""
        return self._max_dispatch.submit_test(user_id)

    @property
    def status(self) -> dict[str, Any]:
        """Возвращает согласованный снимок состояния для HTTP API."""
        with self._lock:
            return {
                "running": self._thread is not None and self._thread.is_alive(),
                "status": self._status,
                "error": self._error,
                "polls": self._polls,
                "snapshot": self._latest,
                "max_poll": self._max_poll,
            }

    def start(self) -> None:
        """Запускает daemon-поток, если опрос ещё не выполняется."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._status = "starting"
            self._error = ""
            self._max_poll = None
            self._thread = threading.Thread(
                target=self._run, name="parse-worker", daemon=True
            )
            self._thread.start()
        self._db.add_log("INFO", "worker", "Парсинг запущен")

    def stop(self) -> None:
        """Запрашивает остановку потока и ждёт её ограниченное время."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=8)
        if thread is not None and thread.is_alive():
            # Не теряем ссылку на ещё работающий поток: иначе повторный start()
            # сможет создать второй конкурентный опрос и две записи в БД.
            message = "Остановка ещё выполняется: браузерная операция не завершена."
            self._set_state("stopping", message)
            self._db.add_log("WARNING", "worker", message)
            return
        with self._lock:
            self._thread = None
            self._status = "stopped"
            self._error = ""
        self._db.add_log("INFO", "worker", "Парсинг остановлен")

    def load_html_file(self, path: Any) -> dict[str, Any]:
        """Показывает в UI снимок из data_test без запуска опроса сайта."""
        from pathlib import Path

        from parsing_site.html_probe import snapshot_from_html_file

        if self.status["running"]:
            raise RuntimeError("Сначала остановите парсинг сайта.")
        target = Path(path)
        settings = self._store.current()
        cfg = settings.to_app_config(load_config())
        snapshot = snapshot_from_html_file(target, cfg.tracking)
        poll_id = self._db.save_snapshot(snapshot)
        table_snapshot_id = self._table_history.save_snapshot(snapshot, source="test")
        payload = snapshot_to_dict(snapshot)
        payload["poll_id"] = poll_id
        payload["table_snapshot_id"] = table_snapshot_id
        with self._lock:
            self._latest = payload
            self._error = ""
            self._status = "stopped"
        self._db.add_log(
            "INFO",
            "probe",
            f"HTML {target.name} импортирован: опрос #{poll_id}, "
            f"динамических {len(snapshot.dynamics)}, строк {len(snapshot.rows)}",
        )
        return payload

    def _set_state(self, status: str, error: str = "") -> None:
        """Атомарно обновляет публичные статус и текст ошибки."""
        with self._lock:
            self._status = status
            self._error = error

    def _run(self) -> None:
        """Подключается к Chrome, опрашивает вкладку и переподключается после ошибок."""
        while not self._stop.is_set():
            # Новое подключение может указывать на другой сайт или другой порядок
            # DOM-маркеров. Не считаем перемещение относительно прежней сессии.
            previous: dict[int, tuple[float, ...]] = {}
            previous_day: str | None = None
            max_alerts = MaxAlertEngine()
            settings = self._store.current()
            cfg = settings.to_app_config(load_config())
            interval = max(0.4, settings.interval_seconds)
            try:
                with BrowserSession(cfg.browser) as session:
                    self._set_state("running")
                    self._db.add_log(
                        "INFO",
                        "worker",
                        f"Подключено к браузеру, сайт: {settings.site}",
                    )
                    fingerprint = (
                        settings.site,
                        settings.cdp_url,
                        settings.wait_for_selector,
                    )
                    self._db.add_log(
                        "INFO", "max", "Рассылка MAX "
                        + (f"включена: {len(settings.max_recipients)} получателей, "
                           f"токен {'доступен' if resolve_max_token(settings) else 'отсутствует'}"
                           if settings.max_enabled else "выключена"),
                    )
                    first_poll = True
                    while not self._stop.is_set():
                        started = monotonic()
                        settings = self._store.current()
                        if (
                            settings.site,
                            settings.cdp_url,
                            settings.wait_for_selector,
                        ) != fingerprint:
                            self._db.add_log(
                                "INFO",
                                "worker",
                                "Переподключение: изменился сайт или CDP",
                            )
                            break
                        cfg = settings.to_app_config(load_config())
                        interval = max(0.4, settings.interval_seconds)
                        archive = CaptureArchive(settings)
                        session.prepare_numbered_capture(
                            (settings.screenshot_width, settings.screenshot_height)
                            if settings.screenshot_enabled or settings.max_enabled else None,
                            html_enabled=settings.archive_html_enabled,
                            on_capture=archive.save,
                        )
                        snapshot = capture_snapshot(session, cfg.tracking)
                        current_day = datetime.now().astimezone().date().isoformat()
                        if current_day != previous_day:
                            previous = {}
                            previous_day = current_day
                            self._db.prune_dynamic_history()
                            self._table_history.prune_dynamic_history()
                        poll_id = self._db.save_snapshot(snapshot, previous)
                        self._table_history.save_snapshot(snapshot)
                        with self._lock:
                            self._polls += 1
                            self._latest = snapshot_to_dict(snapshot)
                            self._latest["poll_id"] = poll_id
                            self._max_poll = None
                            self._status = "running"
                            self._error = ""
                        deliveries = max_alerts.collect(snapshot, settings, archive.screenshots)
                        capture_pair = getattr(session, "capture_pair_view", None)
                        attempted: set[tuple[int, tuple[float, float]]] = set()
                        dynamics = {item.index: item for item in snapshot.dynamics}
                        photo_deadline = monotonic() + 25
                        priority = {"alert": 0, "warning": 1, "blue": 2}
                        row_status = {
                            (row.dynamic_index, row.static_index): row.status
                            for row in snapshot.rows
                        }
                        for delivery in sorted(deliveries, key=lambda item: priority.get(
                            row_status.get((item.dynamic_index, item.nearest_static_index)), 3,
                        )):
                            static = delivery.nearest_static
                            dynamic = dynamics.get(delivery.dynamic_index)
                            if (delivery.screenshot is not None or static is None
                                    or dynamic is None or dynamic.lat is None
                                    or dynamic.lng is None or not callable(capture_pair)):
                                continue
                            key = (delivery.dynamic_index, static)
                            if key not in attempted:
                                if len(attempted) >= 6 or monotonic() >= photo_deadline:
                                    continue
                                attempted.add(key)
                                try:
                                    capture_pair(
                                        delivery.dynamic_index, (dynamic.lat, dynamic.lng),
                                        static, cfg.tracking,
                                    )
                                except Exception as exc:  # noqa: BLE001 — текст остаётся в очереди.
                                    archive.errors.append(f"max: дополнительный кадр: {exc}")
                            delivery.screenshot = max_alerts.select_screenshot(
                                delivery.dynamic_index, static, delivery.related_statics,
                                archive.screenshots, datetime.now().astimezone(),
                            )
                            if delivery.screenshot is not None:
                                delivery.text = delivery.text.removesuffix("\n" + PHOTO_MISSING_NOTE)
                        annotator = getattr(session, "annotate_static_point", None)
                        annotated: dict[tuple[str, int], Any] = {}
                        if callable(annotator):
                            for delivery in deliveries:
                                if delivery.screenshot is None or delivery.nearest_static is None:
                                    continue
                                key = (str(delivery.screenshot), delivery.nearest_static_index)
                                if key not in annotated:
                                    if monotonic() >= photo_deadline:
                                        delivery.screenshot = None
                                        if PHOTO_MISSING_NOTE not in delivery.text:
                                            note = "\nЛимит времени на подготовку скриншотов исчерпан."
                                            delivery.text = delivery.text[:4000 - len(note)] + note
                                        continue
                                    source = next(
                                        (item for item in archive.screenshots
                                         if item.path == delivery.screenshot), None,
                                    )
                                    if source is None or source.view is None:
                                        continue
                                    target = source.path.with_name(
                                        f"{source.path.stem}_max-{delivery.nearest_static_index}.jpg"
                                    )
                                    temporary = target.with_suffix(".jpg.tmp")
                                    try:
                                        image = annotator(
                                            source.path.read_bytes(), source.view,
                                            delivery.nearest_static,
                                            delivery.nearest_static_name,
                                        )
                                        temporary.write_bytes(image)
                                        temporary.replace(target)
                                        archive.saved.append(target)
                                        annotated[key] = target
                                    except Exception as exc:  # noqa: BLE001 — текст доставляется без фото.
                                        archive.errors.append(
                                            f"max: не удалось подписать точку «{delivery.nearest_static_name}»: {exc}"
                                        )
                                        annotated[key] = None
                                    finally:
                                        temporary.unlink(missing_ok=True)
                                delivery.screenshot = annotated[key]
                                if delivery.screenshot is None and PHOTO_MISSING_NOTE not in delivery.text:
                                    note = "\nНе удалось подготовить скриншот с ближайшей точкой."
                                    delivery.text = delivery.text[:4000 - len(note)] + note
                        saved, archive_errors = archive.finish(session)
                        for path in saved:
                            self._db.add_log("INFO", "archive", f"Сохранён файл: {path}")
                        for error in archive_errors:
                            self._db.add_log("WARNING", "archive", error)
                        zone_pairs = {
                            zone: sum(row.status == zone for row in snapshot.rows)
                            for zone in ("blue", "warning", "alert")
                        }
                        selected_rules = [
                            {rule.static_index: set(rule.zones) for rule in recipient.rules}
                            for recipient in settings.max_recipients
                        ]
                        eligible_pairs = sum(
                            row.status in rules.get(row.static_index, ())
                            for row in snapshot.rows for rules in selected_rules
                        )
                        max_poll = {
                            "poll_id": poll_id,
                            "zone_pairs": zone_pairs,
                            "eligible_pairs": eligible_pairs,
                            "prepared": len(deliveries),
                            "prepared_with_screenshot": sum(
                                delivery.screenshot is not None for delivery in deliveries
                            ),
                            "screenshots": len(archive.screenshots),
                            "scan_status": snapshot.scan_status,
                            "archive_errors": archive_errors[:5],
                        }
                        if first_poll and settings.max_enabled:
                            blue = sum(row.status == "blue" for row in snapshot.rows)
                            yellow = sum(row.status == "warning" for row in snapshot.rows)
                            red = sum(row.status == "alert" for row in snapshot.rows)
                            self._db.add_log(
                                "INFO", "max", f"Первый опрос #{poll_id}: "
                                f"синих пар {blue}, жёлтых {yellow}, красных {red}; "
                                f"подготовлено уведомлений {len(deliveries)}",
                            )
                        first_poll = False
                        if deliveries:
                            self._db.add_log(
                                "INFO", "max", f"Опрос #{poll_id}: в очередь добавлено "
                                f"{len(deliveries)} уведомлений для "
                                f"{len({item.user_id for item in deliveries})} получателей",
                            )
                        for delivery in deliveries:
                            self._max_dispatch.submit(delivery)
                        previous = {
                            item.index: (item.x, item.y, item.lat, item.lng)
                            for item in snapshot.dynamics
                        }
                        alerts = sum(
                            1 for row in snapshot.rows if row.status == "alert"
                        )
                        with self._lock:
                            self._max_poll = max_poll
                        if alerts:
                            self._db.add_log(
                                "WARNING",
                                "tracking",
                                f"Опрос #{poll_id}: {alerts} пар ближе порога",
                            )
                        remaining = interval - (monotonic() - started)
                        if remaining > 0 and self._stop.wait(remaining):
                            break
            except BrowserNotConnectedError as exc:
                self._fail("ERROR", str(exc), retry=interval)
            # Worker является долгоживущей границей: ошибка одного опроса логируется,
            # после чего цикл делает паузу и восстанавливается автоматически.
            except Exception as exc:  # noqa: BLE001
                self._fail("ERROR", f"Ошибка парсинга: {exc}", retry=interval)
        self._set_state("stopped")

    def _fail(self, level: str, message: str, retry: float) -> None:
        """Фиксирует ошибку и делает прерываемую паузу перед новой попыткой."""
        self._set_state("error", message)
        self._db.add_log(level, "worker", message)
        self._stop.wait(max(2.0, min(retry, 10.0)))
