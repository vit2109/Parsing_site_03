"""Отдельная SQLite-база с полной историей таблицы главного экрана."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from parsing_site.config import ROOT
from parsing_site.tracking import TrackingSnapshot, row_to_dict

DEFAULT_TABLE_HISTORY_PATH = ROOT / "data" / "table_history.db"
SECONDS_PER_DAY = 86_400
DEFAULT_BYTES_PER_ROW = 320
SNAPSHOT_OVERHEAD_BYTES = 256


class TableHistoryDatabase:
    """Сохраняет снимки основной таблицы независимо от tracking.db."""

    def __init__(self, path: Path | None = None) -> None:
        """Открывает отдельную SQLite-базу в WAL-режиме и создаёт схему."""
        self.path = path or DEFAULT_TABLE_HISTORY_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        """Создаёт таблицы снимков, строк и индексы времени."""
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS table_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    captured_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    url TEXT,
                    title TEXT,
                    row_count INTEGER NOT NULL,
                    estimated_bytes INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS table_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id INTEGER NOT NULL,
                    captured_at TEXT NOT NULL,
                    static_index INTEGER NOT NULL,
                    static_name TEXT NOT NULL,
                    static_coords TEXT NOT NULL,
                    dynamic_name TEXT NOT NULL,
                    dynamic_index INTEGER NOT NULL,
                    dynamic_coords TEXT NOT NULL,
                    distance REAL NOT NULL,
                    distance_unit TEXT NOT NULL,
                    alert_count INTEGER NOT NULL,
                    warning_count INTEGER NOT NULL,
                    nearby_counts TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    FOREIGN KEY(snapshot_id) REFERENCES table_snapshots(id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_table_snapshots_time
                    ON table_snapshots(captured_at);
                CREATE INDEX IF NOT EXISTS idx_table_history_time
                    ON table_history(captured_at);
                CREATE INDEX IF NOT EXISTS idx_table_history_snapshot
                    ON table_history(snapshot_id);
                """
            )
            self._conn.commit()

    def close(self) -> None:
        """Закрывает соединение с историей основной таблицы."""
        with self._lock:
            self._conn.close()

    def prune_dynamic_history(self, keep_days: int = 20) -> dict[str, int | str]:
        """Оставляет в истории таблицы последние календарные дни."""
        cutoff = (date.today() - timedelta(days=max(1, keep_days) - 1)).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM table_snapshots WHERE source IN ('real', 'test') "
                "AND captured_at < ?", (cutoff,)
            )
            self._conn.commit()
        return {"cutoff": cutoff, "snapshots_deleted": cursor.rowcount}

    def compact(self) -> None:
        """Физически уменьшает файл SQLite после ручной очистки."""
        with self._lock:
            self._conn.execute("VACUUM")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def save_snapshot(
        self,
        snapshot: TrackingSnapshot,
        *,
        source: str = "real",
        captured_at: str | None = None,
    ) -> int:
        """Записывает один снимок и все строки, показанные в основной таблице."""
        ts = captured_at or datetime.now().astimezone().isoformat(timespec="seconds")
        rows = [row_to_dict(row) for row in snapshot.rows]
        estimated_bytes = _estimate_snapshot_bytes(rows)
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO table_snapshots("
                "captured_at, source, url, title, row_count, estimated_bytes) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    source,
                    snapshot.url,
                    snapshot.title,
                    len(rows),
                    estimated_bytes,
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            self._conn.executemany(
                "INSERT INTO table_history("
                "snapshot_id, captured_at, static_index, static_name, static_coords, "
                "dynamic_name, dynamic_index, dynamic_coords, distance, distance_unit, "
                "alert_count, warning_count, nearby_counts, status, phone) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        snapshot_id,
                        ts,
                        row["static_index"],
                        row["static_name"],
                        row["static_coords"],
                        row["dynamic_name"],
                        row["dynamic_index"],
                        row["dynamic_coords"],
                        row["distance"],
                        row["distance_unit"],
                        row["alert_count"],
                        row["warning_count"],
                        row["nearby_counts"],
                        row["status"],
                        row["phone"],
                    )
                    for row in rows
                ],
            )
            self._conn.commit()
        return snapshot_id

    def storage_stats(
        self,
        *,
        interval_seconds: float,
        current_rows: int | None = None,
    ) -> dict[str, int | float | str]:
        """Текущий размер и прогноз объёма истории на 24 часа."""
        interval = max(0.4, float(interval_seconds))
        with self._lock:
            totals = self._conn.execute(
                "SELECT COUNT(*) AS snapshots, COALESCE(SUM(row_count), 0) AS rows "
                "FROM table_snapshots"
            ).fetchone()
            recent = self._conn.execute(
                "SELECT row_count, estimated_bytes FROM table_snapshots "
                "ORDER BY id DESC LIMIT 20"
            ).fetchall()

        recent_rows = sum(int(item["row_count"]) for item in recent)
        recent_bytes = sum(int(item["estimated_bytes"]) for item in recent)
        bytes_per_row = (
            max(DEFAULT_BYTES_PER_ROW, recent_bytes // recent_rows)
            if recent_rows
            else DEFAULT_BYTES_PER_ROW
        )
        if current_rows is None:
            rows_per_snapshot = round(recent_rows / len(recent)) if recent else 0
        else:
            rows_per_snapshot = max(0, int(current_rows))
        snapshots_per_day = max(1, round(SECONDS_PER_DAY / interval))
        rows_per_day = snapshots_per_day * rows_per_snapshot
        bytes_per_snapshot = SNAPSHOT_OVERHEAD_BYTES + rows_per_snapshot * bytes_per_row
        return {
            "database": self.path.name,
            "current_size_bytes": _database_size(self.path),
            "total_snapshots": int(totals["snapshots"]),
            "total_rows": int(totals["rows"]),
            "interval_seconds": interval,
            "snapshots_per_day": snapshots_per_day,
            "rows_per_snapshot": rows_per_snapshot,
            "estimated_rows_per_day": rows_per_day,
            "estimated_bytes_per_day": snapshots_per_day * bytes_per_snapshot,
        }

    def list_rows(self, limit: int = 100) -> list[dict[str, Any]]:
        """Возвращает последние строки для диагностики и тестов."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM table_history ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 10_000)),),
            ).fetchall()
        return [dict(row) for row in rows]


def _estimate_snapshot_bytes(rows: list[dict[str, object]]) -> int:
    """Оценивает дисковый объём снимка по строковым полям и накладным расходам."""
    size = SNAPSHOT_OVERHEAD_BYTES
    text_fields = (
        "static_name",
        "static_coords",
        "dynamic_name",
        "dynamic_coords",
        "distance_unit",
        "nearby_counts",
        "status",
        "phone",
    )
    for row in rows:
        text_size = sum(
            len(str(row.get(field) or "").encode("utf-8")) for field in text_fields
        )
        size += max(DEFAULT_BYTES_PER_ROW, 176 + text_size)
    return size


def _database_size(path: Path) -> int:
    """Суммирует размеры основного SQLite-файла и активного WAL."""
    return sum(
        candidate.stat().st_size
        for candidate in (path, Path(f"{path}-wal"))
        if candidate.exists()
    )
