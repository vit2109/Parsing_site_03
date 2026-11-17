"""Порционное обслуживание истории вне запуска HTTP и потока парсинга."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic

from parsing_site.settings_store import RuntimeSettings

logger = logging.getLogger("uvicorn.error")
BATCH_SIZE = 5
BATCH_TIMEOUT_SECONDS = 5.0


class HistoryMaintenance:
    """Удаляет старые снимки через отдельные WAL-соединения короткими транзакциями."""

    def __init__(self, tracking_path: Path, table_path: Path,
                 current_settings: Callable[[], RuntimeSettings]) -> None:
        self._tracking_path = tracking_path
        self._table_path = table_path
        self._current_settings = current_settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = datetime.now().astimezone().isoformat(timespec="seconds")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="history-maintenance", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=BATCH_TIMEOUT_SECONDS + 2)
            if self._thread.is_alive():
                logger.warning("Обслуживание истории ещё завершает текущую транзакцию")

    def _execute(self, path: Path, query: str, parameters: tuple) -> int:
        # Не захватываем блокировки рабочих Database/TableHistoryDatabase.
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=0.5)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            deadline = monotonic() + BATCH_TIMEOUT_SECONDS
            connection.set_progress_handler(
                lambda: int(self._stop.is_set() or monotonic() >= deadline), 1000,
            )
            with connection:
                result = connection.execute(query, parameters)
                return result.rowcount
        finally:
            connection.close()

    def _prune(self, path: Path, *, table_history: bool) -> int:
        table, time_column, source_column, sources = (
            ("table_snapshots", "captured_at", "source", "('real', 'test')")
            if table_history else ("polls", "ts", "data_source", "('real')")
        )
        # Имена таблиц — константы приложения, пользовательские значения параметризованы.
        query = (f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} "
                 f"WHERE {source_column} IN {sources} AND {time_column} < ? "
                 f"ORDER BY {time_column}, id LIMIT ?)")
        total = 0
        while not self._stop.is_set():
            days = max(20, self._current_settings().history_keep_days)
            cutoff = (datetime.now().astimezone().date() - timedelta(days=days - 1)).isoformat()
            count = self._execute(path, query, (cutoff, BATCH_SIZE))
            total += count
            if count < BATCH_SIZE or self._stop.wait(0.1):
                break
        return total

    def _run(self) -> None:
        # Даём Uvicorn закончить startup и открыть порт до начала обслуживания.
        if self._stop.wait(1):
            return
        try:
            self._execute(
                self._tracking_path,
                "UPDATE max_deliveries SET finished_at = ?, status = 'skipped', "
                "error = 'Приложение завершилось до отправки' "
                "WHERE status = 'queued' AND queued_at < ?",
                (datetime.now().astimezone().isoformat(timespec="seconds"), self._started_at),
            )
        except sqlite3.Error:
            if not self._stop.is_set():
                logger.exception("Не удалось восстановить статусы очереди MAX; интерфейс продолжает работать")
        serviced_day = None
        while not self._stop.is_set():
            today = datetime.now().astimezone().date()
            if today != serviced_day:
                for path, table_history in ((self._tracking_path, False), (self._table_path, True)):
                    if self._stop.is_set():
                        return
                    started = monotonic()
                    logger.info("Обслуживание: очистка %s небольшими порциями", path.name)
                    try:
                        deleted = self._prune(path, table_history=table_history)
                    except sqlite3.Error:
                        if not self._stop.is_set():
                            logger.exception(
                                "Очистка %s отложена: база занята или превышено время операции. "
                                "Интерфейс продолжает работать; доступна ручная очистка в настройках", path.name,
                            )
                    else:
                        logger.info("Обслуживание: %s, удалено снимков %s за %.1f с",
                                    path.name, deleted, monotonic() - started)
                serviced_day = today
            self._stop.wait(30)
