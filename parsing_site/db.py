"""SQLite: логи, опросы, перемещения объектов, сохранённые настройки UI."""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta
from math import cos, pi, sin
from pathlib import Path
from typing import Any

from parsing_site.config import ROOT
from parsing_site.coordinates import haversine_km
from parsing_site.tracking import BLUE_ZONE_KM, TrackingSnapshot

DEFAULT_DB_PATH = ROOT / "data" / "tracking.db"


class Database:
    """Потокобезопасное хранилище настроек и нормализованной истории опросов."""

    def __init__(self, path: Path | None = None) -> None:
        """Открывает БД, создаёт схему и пытается спасти данные при повреждении."""
        self.path = path or DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.recovery_notice: str | None = None
        try:
            self._conn = self._open_connection()
            self.init_schema()
        except sqlite3.DatabaseError as exc:
            if not _is_corrupt_database_error(exc):
                if hasattr(self, "_conn"):
                    self._conn.close()
                raise
            if hasattr(self, "_conn"):
                self._conn.close()
            settings, logs, backup = _salvage_corrupt_database(self.path)
            self._conn = self._open_connection()
            self.init_schema()
            self._restore_salvaged_data(settings, logs)
            self.recovery_notice = (
                "Повреждённая старая БД восстановлена. Настройки и стационарные "
                f"объекты перенесены; исходный файл сохранён: {backup.name}"
            )
            self.add_log("WARNING", "migration", self.recovery_notice)

    def _open_connection(self) -> sqlite3.Connection:
        """Открывает SQLite в WAL-режиме и регистрирует функцию расстояния."""
        connection = sqlite3.connect(self.path, check_same_thread=False)
        try:
            connection.row_factory = sqlite3.Row
            connection.create_function("haversine_km", 4, _sqlite_haversine_km)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except Exception:
            connection.close()
            raise

    def _restore_salvaged_data(
        self,
        settings: list[tuple[str, str]],
        logs: list[tuple[str, str, str, str]],
    ) -> None:
        """Возвращает читаемые настройки и логи в новую БД после восстановления."""
        with self._lock:
            self._conn.executemany(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                settings,
            )
            self._conn.executemany(
                "INSERT INTO logs(ts, level, source, message) VALUES(?, ?, ?, ?)",
                logs,
            )
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES('storage_schema', '2') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            self._conn.execute(
                "INSERT INTO settings(key, value) "
                "VALUES('legacy_dynamic_cleanup_v1', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            self._conn.commit()

    def close(self) -> None:
        """Закрывает общее SQLite-соединение под блокировкой."""
        with self._lock:
            self._conn.close()

    def prune_dynamic_history(self, keep_days: int = 20) -> dict[str, int | str]:
        """Удаляет старые реальные опросы; настройки статичных точек не затрагивает."""
        cutoff = (date.today() - timedelta(days=max(1, keep_days) - 1)).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM polls WHERE data_source = 'real' AND ts < ?",
                (cutoff,),
            )
            self._conn.commit()
        return {"cutoff": cutoff, "polls_deleted": cursor.rowcount}

    def compact(self) -> None:
        """Возвращает операционной системе место после ручной очистки."""
        with self._lock:
            self._conn.execute("VACUUM")
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def init_schema(self) -> None:
        """Создаёт актуальную схему и выполняет одноразовые миграции."""
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS polls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    url TEXT,
                    title TEXT,
                    dynamics_count INTEGER NOT NULL DEFAULT 0,
                    statics_count INTEGER NOT NULL DEFAULT 0,
                    alerts INTEGER NOT NULL DEFAULT 0,
                    warnings INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS static_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    poll_id INTEGER NOT NULL,
                    static_index INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    x REAL NOT NULL,
                    y REAL NOT NULL,
                    lat REAL,
                    lng REAL,
                    UNIQUE(poll_id, static_index),
                    FOREIGN KEY(poll_id) REFERENCES polls(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS dynamic_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    poll_id INTEGER NOT NULL,
                    dynamic_index INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    x REAL NOT NULL,
                    y REAL NOT NULL,
                    lat REAL,
                    lng REAL,
                    dx REAL,
                    dy REAL,
                    distance_moved REAL,
                    color TEXT,
                    UNIQUE(poll_id, dynamic_index),
                    FOREIGN KEY(poll_id) REFERENCES polls(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
                CREATE INDEX IF NOT EXISTS idx_polls_ts ON polls(ts);
                CREATE INDEX IF NOT EXISTS idx_static_observations_object
                    ON static_observations(static_index, poll_id);
                CREATE INDEX IF NOT EXISTS idx_dynamic_observations_object
                    ON dynamic_observations(dynamic_index, poll_id);
                """
            )
            self._ensure_poll_columns()
            self._ensure_legacy_columns()
            self._conn.commit()
            compact = self._migrate_legacy_storage()
            cleaned = self._cleanup_legacy_dynamics()
            if compact or cleaned:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _ensure_poll_columns(self) -> None:
        """Добавляет отсутствующие поля в таблицу опросов старой версии."""
        existing = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(polls)")
        }
        columns = {
            "coordinate_mode": "TEXT",
            "distance_unit": "TEXT",
            "threshold": "REAL",
            "warning_limit": "REAL",
            "blue_limit": f"REAL NOT NULL DEFAULT {BLUE_ZONE_KM}",
            "map_width": "REAL",
            "map_height": "REAL",
            "map_zoom": "REAL",
            "map_center_lat": "REAL",
            "map_center_lng": "REAL",
            "geo_source": "TEXT",
            "data_source": "TEXT NOT NULL DEFAULT 'real'",
            "legacy_migrated": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in columns.items():
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE polls ADD COLUMN {column} {declaration}"
                )

    def _table_exists(self, name: str) -> bool:
        """Проверяет наличие таблицы в текущей SQLite-схеме."""
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    def _ensure_legacy_columns(self) -> None:
        """Дополняет старую таблицу расстояний географическими полями."""
        if not self._table_exists("distance_rows"):
            return
        existing = {
            str(row[1])
            for row in self._conn.execute("PRAGMA table_info(distance_rows)")
        }
        for column in ("static_lat", "static_lng", "dynamic_lat", "dynamic_lng"):
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE distance_rows ADD COLUMN {column} REAL"
                )

    def _migrate_legacy_storage(self) -> bool:
        """Переносит опросы и статики; старую неполную динамику не копирует."""
        if not self._table_exists("distance_rows"):
            return False
        legacy_count = int(
            self._conn.execute("SELECT COUNT(*) FROM distance_rows").fetchone()[0]
        )
        if legacy_count == 0:
            self._conn.execute("DROP TABLE distance_rows")
            if self._table_exists("movements"):
                self._conn.execute("DROP TABLE movements")
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES('storage_schema', '2') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            self._conn.commit()
            return True

        try:
            self._conn.execute("BEGIN IMMEDIATE")
            expected_statics = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM (SELECT poll_id, static_index "
                    "FROM distance_rows WHERE poll_id IS NOT NULL "
                    "GROUP BY poll_id, static_index)"
                ).fetchone()[0]
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO static_observations("
                "poll_id, static_index, name, x, y, lat, lng) "
                "SELECT poll_id, static_index, MIN(static_name), MIN(static_x), "
                "MIN(static_y), MAX(static_lat), MAX(static_lng) "
                "FROM distance_rows WHERE poll_id IS NOT NULL "
                "GROUP BY poll_id, static_index"
            )
            self._conn.execute(
                "UPDATE polls SET legacy_migrated = 1 WHERE id IN "
                "(SELECT DISTINCT poll_id FROM distance_rows "
                "WHERE poll_id IS NOT NULL)"
            )
            migrated_statics = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM static_observations WHERE poll_id IN "
                    "(SELECT id FROM polls WHERE legacy_migrated = 1)"
                ).fetchone()[0]
            )
            if migrated_statics != expected_statics:
                raise RuntimeError(
                    "Миграция остановлена: количество стационарных наблюдений "
                    f"не совпало ({migrated_statics} != {expected_statics})."
                )
            self._conn.execute("DROP INDEX IF EXISTS idx_distance_history")
            self._conn.execute("DROP INDEX IF EXISTS idx_movements_ts")
            self._conn.execute("DROP INDEX IF EXISTS idx_movements_object")
            self._conn.execute("DROP TABLE distance_rows")
            if self._table_exists("movements"):
                self._conn.execute("DROP TABLE movements")
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES('storage_schema', '2') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            self._conn.commit()
            return True
        except Exception:
            self._conn.rollback()
            raise

    def _cleanup_legacy_dynamics(self) -> bool:
        """Удаляет старую динамику, не затрагивая новые и тестовые опросы."""
        marker = self._conn.execute(
            "SELECT value FROM settings WHERE key = 'legacy_dynamic_cleanup_v1'"
        ).fetchone()
        if marker is not None and str(marker[0]) == "1":
            return False
        self._conn.execute(
            "UPDATE polls SET legacy_migrated = 1 WHERE data_source = 'real' "
            "AND coordinate_mode IS NULL AND id IN "
            "(SELECT DISTINCT poll_id FROM static_observations)"
        )
        cursor = self._conn.execute(
            "DELETE FROM dynamic_observations WHERE poll_id IN "
            "(SELECT id FROM polls WHERE legacy_migrated = 1)"
        )
        self._conn.execute(
            "INSERT INTO settings(key, value) "
            "VALUES('legacy_dynamic_cleanup_v1', '1') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        self._conn.commit()
        return int(cursor.rowcount or 0) > 0

    def get_setting(self, key: str) -> str | None:
        """Читает строковое значение настройки по ключу."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row["value"])

    def set_setting(self, key: str, value: str) -> None:
        """Атомарно создаёт или обновляет строковую настройку."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def add_log(self, level: str, source: str, message: str) -> None:
        """Добавляет датированное сообщение во внутренний журнал приложения."""
        ts = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO logs(ts, level, source, message) VALUES(?, ?, ?, ?)",
                (ts, level.upper(), source, message),
            )
            self._conn.commit()

    def list_logs(self, limit: int = 80) -> list[dict[str, Any]]:
        """Возвращает последние сообщения журнала, начиная с нового."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, ts, level, source, message FROM logs "
                "ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_snapshot(
        self,
        snapshot: TrackingSnapshot,
        previous: dict[int, tuple[float, ...]] | None = None,
    ) -> int:
        """Пишет опрос и по одной записи на каждый статичный/динамический объект."""
        ts = _now()
        prev = previous or {}
        alerts = sum(1 for row in snapshot.rows if row.status == "alert")
        warnings = sum(1 for row in snapshot.rows if row.status == "warning")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO polls(ts, url, title, dynamics_count, statics_count, "
                "alerts, warnings, coordinate_mode, distance_unit, threshold, "
                "warning_limit, blue_limit, map_width, map_height, map_zoom, "
                "map_center_lat, "
                "map_center_lng, geo_source, data_source) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    snapshot.url,
                    snapshot.title,
                    len(snapshot.dynamics),
                    len(snapshot.statics),
                    alerts,
                    warnings,
                    snapshot.coordinate_mode,
                    snapshot.distance_unit,
                    snapshot.threshold,
                    snapshot.warning_limit,
                    snapshot.blue_limit,
                    snapshot.map_size[0] if snapshot.map_size else None,
                    snapshot.map_size[1] if snapshot.map_size else None,
                    snapshot.map_zoom,
                    snapshot.map_center[0] if snapshot.map_center else None,
                    snapshot.map_center[1] if snapshot.map_center else None,
                    snapshot.geo_source,
                    "real",
                ),
            )
            poll_id = int(cursor.lastrowid)
            self._conn.executemany(
                "INSERT INTO static_observations("
                "poll_id, static_index, name, x, y, lat, lng) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        poll_id,
                        item.index,
                        item.name,
                        item.x,
                        item.y,
                        item.lat,
                        item.lng,
                    )
                    for item in snapshot.statics
                ],
            )
            for item in snapshot.dynamics:
                dx = dy = moved = None
                if item.index in prev:
                    prior = prev[item.index]
                    px, py = float(prior[0]), float(prior[1])
                    plat = (
                        float(prior[2])
                        if len(prior) > 2 and prior[2] is not None
                        else None
                    )
                    plng = (
                        float(prior[3])
                        if len(prior) > 3 and prior[3] is not None
                        else None
                    )
                    if (
                        item.lat is not None
                        and item.lng is not None
                        and plat is not None
                        and plng is not None
                    ):
                        dy = item.lat - plat
                        dx = item.lng - plng
                        moved = haversine_km(plat, plng, item.lat, item.lng)
                    else:
                        dx = item.x - px
                        dy = item.y - py
                        moved = (dx * dx + dy * dy) ** 0.5
                self._conn.execute(
                    "INSERT INTO dynamic_observations("
                    "poll_id, dynamic_index, name, x, y, lat, lng, dx, dy, "
                    "distance_moved, color) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        poll_id,
                        item.index,
                        item.name,
                        item.x,
                        item.y,
                        item.lat,
                        item.lng,
                        dx,
                        dy,
                        moved,
                        item.color,
                    ),
                )
            self._conn.commit()
        return poll_id

    def ensure_fake_history(self, *, force: bool = False) -> dict[str, int]:
        """Создаёт компактный детерминированный набор для проверки хронологии."""
        with self._lock:
            existing = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM polls WHERE data_source = 'fake'"
                ).fetchone()[0]
            )
            version = self._conn.execute(
                "SELECT value FROM settings WHERE key = 'fake_history_version'"
            ).fetchone()
            if not force and existing and version and str(version[0]) == "1":
                return self._fake_history_counts()

            self._conn.execute("DELETE FROM polls WHERE data_source = 'fake'")
            statics = [
                (1, "Демо: Москва", 55.7558, 37.6173),
                (2, "Демо: Тверь", 56.8587, 35.9176),
                (3, "Демо: Рязань", 54.6296, 39.7416),
            ]
            dynamics = [
                (1, "Демо-машина 1", "#ff6b6b", 0.08),
                (2, "Демо-машина 2", "#ffd166", 0.25),
                (3, "Демо-машина 3", "#06d6a0", 0.70),
                (4, "Демо-машина 4", "#4dabf7", 1.50),
                (5, "Демо-машина 5", "#b197fc", 3.00),
                (6, "Демо-машина 6", "#f783ac", 5.00),
            ]
            center_lat, center_lng = 55.7558, 37.6173
            start = datetime.now().astimezone().replace(
                minute=0, second=0, microsecond=0
            ) - timedelta(days=14)
            previous: dict[int, tuple[float, float]] = {}
            for step in range(169):
                ts = (start + timedelta(hours=2 * step)).isoformat(timespec="seconds")
                positions = []
                for dynamic_index, name, color, radius in dynamics:
                    anchor = statics[(dynamic_index - 1) % len(statics)]
                    phase = 2 * pi * (step / 36 + dynamic_index / len(dynamics))
                    lat = anchor[2] + radius * sin(phase)
                    lng = anchor[3] + radius * cos(phase) / max(
                        0.2, cos(anchor[2] * pi / 180)
                    )
                    x, y = _demo_xy(lat, lng, center_lat, center_lng)
                    prior = previous.get(dynamic_index)
                    dx = lng - prior[1] if prior else 0.0
                    dy = lat - prior[0] if prior else 0.0
                    moved = haversine_km(prior[0], prior[1], lat, lng) if prior else 0.0
                    previous[dynamic_index] = (lat, lng)
                    positions.append(
                        (dynamic_index, name, color, x, y, lat, lng, dx, dy, moved)
                    )
                distances = [
                    haversine_km(slat, slng, dlat, dlng)
                    for _, _, slat, slng in statics
                    for _, _, _, _, _, dlat, dlng, _, _, _ in positions
                ]
                cursor = self._conn.execute(
                    "INSERT INTO polls(ts, url, title, dynamics_count, statics_count, "
                    "alerts, warnings, coordinate_mode, distance_unit, threshold, "
                    "warning_limit, blue_limit, map_width, map_height, map_zoom, "
                    "map_center_lat, "
                    "map_center_lng, geo_source, data_source) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ts,
                        "demo://history",
                        "Тестовая карта хронологии",
                        len(dynamics),
                        len(statics),
                        sum(distance <= 50 for distance in distances),
                        sum(50 < distance <= 150 for distance in distances),
                        "latlng",
                        "km",
                        50.0,
                        150.0,
                        BLUE_ZONE_KM,
                        1000.0,
                        800.0,
                        7.0,
                        center_lat,
                        center_lng,
                        "demo",
                        "fake",
                    ),
                )
                poll_id = int(cursor.lastrowid)
                self._conn.executemany(
                    "INSERT INTO static_observations("
                    "poll_id, static_index, name, x, y, lat, lng) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            poll_id,
                            index,
                            name,
                            *_demo_xy(lat, lng, center_lat, center_lng),
                            lat,
                            lng,
                        )
                        for index, name, lat, lng in statics
                    ],
                )
                self._conn.executemany(
                    "INSERT INTO dynamic_observations("
                    "poll_id, dynamic_index, name, color, x, y, lat, lng, dx, dy, "
                    "distance_moved) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            poll_id,
                            dynamic_index,
                            name,
                            color,
                            x,
                            y,
                            lat,
                            lng,
                            dx,
                            dy,
                            moved,
                        )
                        for dynamic_index, name, color, x, y, lat, lng, dx, dy, moved in positions
                    ],
                )
            self._conn.execute(
                "INSERT INTO settings(key, value) VALUES('fake_history_version', '1') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
            self._conn.commit()
            return self._fake_history_counts()

    def _fake_history_counts(self) -> dict[str, int]:
        """Подсчитывает опросы и наблюдения демонстрационного набора."""
        polls = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM polls WHERE data_source = 'fake'"
            ).fetchone()[0]
        )
        dynamics = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM dynamic_observations d JOIN polls p "
                "ON p.id = d.poll_id WHERE p.data_source = 'fake'"
            ).fetchone()[0]
        )
        statics = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM static_observations s JOIN polls p "
                "ON p.id = s.poll_id WHERE p.data_source = 'fake'"
            ).fetchone()[0]
        )
        return {"polls": polls, "dynamics": dynamics, "statics": statics}

    def list_movements(self, limit: int = 200) -> list[dict[str, Any]]:
        """Возвращает последние вычисленные перемещения реальных объектов."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.id, p.ts, d.dynamic_index AS object_index, d.name, "
                "d.x, d.y, d.dx, d.dy, d.distance_moved, d.color, p.url "
                "FROM dynamic_observations d JOIN polls p ON p.id = d.poll_id "
                "WHERE p.data_source = 'real' AND substr(p.ts, 1, 10) = ? "
                "ORDER BY d.id DESC LIMIT ?",
                (date.today().isoformat(), max(1, min(limit, 1000))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["x"] = round(item["x"], 2)
            item["y"] = round(item["y"], 2)
            if item["dx"] is not None:
                item["dx"] = round(item["dx"], 2)
                item["dy"] = round(item["dy"], 2)
                item["distance_moved"] = round(item["distance_moved"] or 0, 2)
            result.append(item)
        return result

    def list_history_statics(self, data_source: str = "real") -> list[dict[str, Any]]:
        """Статичные объекты и объём географической/старой пиксельной истории."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.static_index, MAX(s.name) AS static_name, "
                "MAX(s.lat) AS static_lat, MAX(s.lng) AS static_lng, "
                "MIN(p.ts) AS first_seen, MAX(p.ts) AS last_seen, "
                "COUNT(*) AS samples, "
                "SUM(CASE WHEN s.lat IS NOT NULL AND s.lng IS NOT NULL "
                "THEN 1 ELSE 0 END) AS km_samples, "
                "SUM(CASE WHEN s.lat IS NULL OR s.lng IS NULL "
                "THEN 1 ELSE 0 END) AS legacy_samples, "
                "(SELECT p2.threshold FROM static_observations s2 "
                "JOIN polls p2 ON p2.id = s2.poll_id "
                "WHERE s2.static_index = s.static_index "
                "AND p2.data_source = ? AND p2.distance_unit = 'km' "
                "ORDER BY p2.ts DESC, p2.id DESC LIMIT 1) AS alert_radius_km, "
                "(SELECT p2.warning_limit FROM static_observations s2 "
                "JOIN polls p2 ON p2.id = s2.poll_id "
                "WHERE s2.static_index = s.static_index "
                "AND p2.data_source = ? AND p2.distance_unit = 'km' "
                "ORDER BY p2.ts DESC, p2.id DESC LIMIT 1) AS warning_radius_km, "
                "(SELECT p2.blue_limit FROM static_observations s2 "
                "JOIN polls p2 ON p2.id = s2.poll_id "
                "WHERE s2.static_index = s.static_index "
                "AND p2.data_source = ? AND p2.distance_unit = 'km' "
                "ORDER BY p2.ts DESC, p2.id DESC LIMIT 1) AS blue_radius_km "
                "FROM static_observations s JOIN polls p ON p.id = s.poll_id "
                "WHERE p.data_source = ? GROUP BY s.static_index "
                "ORDER BY static_name, s.static_index",
                (
                    _normalize_source(data_source),
                    _normalize_source(data_source),
                    _normalize_source(data_source),
                    _normalize_source(data_source),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_dynamic_trajectory(
        self,
        *,
        dynamic_index: int,
        date_from: str,
        date_to: str,
        object_date: str | None = None,
        data_source: str = "real",
        limit: int = 20_000,
    ) -> list[dict[str, Any]]:
        """Положения объекта; при указанной дате — только его суточная траектория."""
        day_filter = " AND substr(p.ts, 1, 10) = ?" if object_date else ""
        params: list[Any] = [
            dynamic_index,
            date_from,
            date_to,
            _normalize_source(data_source),
        ]
        if object_date:
            params.append(object_date)
        params.append(max(1, min(limit, 20_000)))
        with self._lock:
            rows = self._conn.execute(
                "SELECT d.id, p.id AS poll_id, p.ts, substr(p.ts, 1, 10) AS "
                "object_date, d.dynamic_index, d.name AS "
                "dynamic_name, d.lat, d.lng, d.color FROM dynamic_observations d "
                "JOIN polls p ON p.id = d.poll_id WHERE d.dynamic_index = ? "
                "AND p.ts >= ? AND p.ts <= ? AND p.data_source = ? "
                + day_filter
                + " "
                "AND d.lat IS NOT NULL AND d.lng IS NOT NULL "
                "ORDER BY p.ts ASC, d.id ASC LIMIT ?",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def get_static_zone_trajectories(
        self,
        *,
        static_index: int,
        date_from: str,
        date_to: str,
        data_source: str = "real",
        limit: int = 20_000,
    ) -> dict[str, Any] | None:
        """Траектории объектов, хотя бы раз вошедших в одну из зон объекта."""
        source = _normalize_source(data_source)
        with self._lock:
            static = self._conn.execute(
                "SELECT s.static_index, s.name AS static_name, s.lat, s.lng, "
                "p.threshold AS alert_radius_km, p.warning_limit AS warning_radius_km, "
                "p.blue_limit AS blue_radius_km "
                "FROM static_observations s JOIN polls p ON p.id = s.poll_id "
                "WHERE s.static_index = ? AND p.data_source = ? "
                "AND p.ts >= ? AND p.ts <= ? AND p.distance_unit = 'km' "
                "AND s.lat IS NOT NULL AND s.lng IS NOT NULL "
                "ORDER BY p.ts DESC, p.id DESC LIMIT 1",
                (static_index, source, date_from, date_to),
            ).fetchone()
            if static is None:
                return None
            rows = self._conn.execute(
                "WITH entered AS ("
                "SELECT DISTINCT substr(p.ts, 1, 10) AS object_date, "
                "d.dynamic_index FROM static_observations s "
                "JOIN dynamic_observations d ON d.poll_id = s.poll_id "
                "JOIN polls p ON p.id = s.poll_id WHERE s.static_index = ? "
                "AND p.ts >= ? AND p.ts <= ? AND p.data_source = ? "
                "AND p.distance_unit = 'km' AND s.lat IS NOT NULL "
                "AND s.lng IS NOT NULL AND d.lat IS NOT NULL AND d.lng IS NOT NULL "
                "AND haversine_km(s.lat, s.lng, d.lat, d.lng) <= p.blue_limit"
                ") SELECT d.id, p.id AS poll_id, p.ts, "
                "substr(p.ts, 1, 10) AS object_date, d.dynamic_index, "
                "d.name AS dynamic_name, d.lat, d.lng, d.color, "
                "haversine_km(s.lat, s.lng, d.lat, d.lng) AS distance, CASE "
                "WHEN p.distance_unit = 'km' AND haversine_km(s.lat, s.lng, d.lat, d.lng) "
                "<= p.threshold THEN 'alert' WHEN p.distance_unit = 'km' AND "
                "haversine_km(s.lat, s.lng, d.lat, d.lng) <= p.warning_limit "
                "THEN 'warning' WHEN p.distance_unit = 'km' AND "
                "haversine_km(s.lat, s.lng, d.lat, d.lng) <= p.blue_limit "
                "THEN 'blue' ELSE 'outside' END AS status "
                "FROM dynamic_observations d JOIN polls p ON p.id = d.poll_id "
                "JOIN static_observations s ON s.poll_id = p.id AND s.static_index = ? "
                "JOIN entered e ON e.dynamic_index = d.dynamic_index "
                "AND e.object_date = substr(p.ts, 1, 10) "
                "WHERE p.ts >= ? AND p.ts <= ? AND p.data_source = ? "
                "AND d.lat IS NOT NULL AND d.lng IS NOT NULL "
                "AND s.lat IS NOT NULL AND s.lng IS NOT NULL "
                "ORDER BY object_date, d.dynamic_index, p.ts ASC, d.id ASC LIMIT ?",
                (
                    static_index,
                    date_from,
                    date_to,
                    source,
                    static_index,
                    date_from,
                    date_to,
                    source,
                    max(1, min(limit, 20_000)),
                ),
            ).fetchall()
        result = dict(static)
        result["points"] = [
            {**dict(row), "distance": round(float(row["distance"]), 3)} for row in rows
        ]
        return result

    def count_legacy_history(
        self,
        *,
        static_index: int,
        date_from: str,
        date_to: str,
        dynamic_query: str = "",
        data_source: str = "real",
    ) -> int:
        """Считает записи без полной географии, исключённые из истории в километрах."""
        where = [
            "s.static_index = ?",
            "p.ts >= ?",
            "p.ts <= ?",
            "p.data_source = ?",
            "(s.lat IS NULL OR s.lng IS NULL OR d.lat IS NULL OR d.lng IS NULL)",
        ]
        params: list[Any] = [
            static_index,
            date_from,
            date_to,
            _normalize_source(data_source),
        ]
        query = dynamic_query.strip()
        if query:
            where.append("(d.name LIKE ? OR CAST(d.dynamic_index AS TEXT) = ?)")
            params.extend([f"%{query}%", query])
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM static_observations s "
                "JOIN dynamic_observations d ON d.poll_id = s.poll_id "
                "JOIN polls p ON p.id = s.poll_id WHERE " + " AND ".join(where),
                params,
            ).fetchone()
        return int(row[0]) if row else 0

    def list_distance_history(
        self,
        *,
        static_index: int,
        date_from: str,
        date_to: str,
        max_distance_km: float,
        dynamic_query: str = "",
        limit: int = 5000,
        data_source: str = "real",
    ) -> list[dict[str, Any]]:
        """Хронология приближений в географическом режиме, от новых к старым."""
        where = [
            "s.static_index = ?",
            "p.ts >= ?",
            "p.ts <= ?",
            "p.data_source = ?",
            "s.lat IS NOT NULL",
            "s.lng IS NOT NULL",
            "d.lat IS NOT NULL",
            "d.lng IS NOT NULL",
        ]
        params: list[Any] = [
            static_index,
            date_from,
            date_to,
            _normalize_source(data_source),
        ]
        query = dynamic_query.strip()
        if query:
            where.append("(d.name LIKE ? OR CAST(d.dynamic_index AS TEXT) = ?)")
            params.extend([f"%{query}%", query])
        params.extend([max_distance_km, max(1, min(limit, 20_000))])
        sql = (
            "SELECT * FROM (SELECT d.id, p.ts, p.id AS poll_id, "
            "substr(p.ts, 1, 10) AS object_date, "
            "s.static_index, s.name AS static_name, s.lat AS static_lat, "
            "s.lng AS static_lng, d.dynamic_index, d.name AS dynamic_name, "
            "d.lat AS dynamic_lat, d.lng AS dynamic_lng, "
            "haversine_km(s.lat, s.lng, d.lat, d.lng) AS distance, "
            "'km' AS distance_unit, CASE "
            "WHEN p.distance_unit = 'km' AND haversine_km(s.lat, s.lng, d.lat, d.lng) "
            "<= p.threshold THEN 'alert' "
            "WHEN p.distance_unit = 'km' AND haversine_km(s.lat, s.lng, d.lat, d.lng) "
            "<= p.warning_limit THEN 'warning' "
            "WHEN p.distance_unit = 'km' AND "
            "haversine_km(s.lat, s.lng, d.lat, d.lng) <= p.blue_limit "
            "THEN 'blue' ELSE 'ok' END AS status "
            "FROM static_observations s "
            "JOIN dynamic_observations d ON d.poll_id = s.poll_id "
            "JOIN polls p ON p.id = s.poll_id WHERE "
            + " AND ".join(where)
            + ") WHERE distance <= ? "
            "ORDER BY ts DESC, distance ASC, id DESC LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["distance"] = round(float(item["distance"]), 3)
            result.append(item)
        return result


def _now() -> str:
    """Возвращает локальное время с часовым поясом в ISO-формате."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _is_corrupt_database_error(exc: sqlite3.DatabaseError) -> bool:
    """Отличает повреждение файла от обычной ошибки запроса или схемы."""
    message = str(exc).lower()
    return "malformed" in message or "not a database" in message


def _salvage_corrupt_database(
    path: Path,
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, str]], Path]:
    """Сохраняет читаемые настройки/логи и освобождает имя для чистой БД."""
    settings: list[tuple[str, str]] = []
    logs: list[tuple[str, str, str, str]] = []
    source: sqlite3.Connection | None = None
    try:
        source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = source.execute("SELECT key, value FROM settings").fetchall()
            settings = [(str(row[0]), str(row[1])) for row in rows]
        except sqlite3.DatabaseError:
            pass
        try:
            rows = source.execute(
                "SELECT ts, level, source, message FROM logs "
                "ORDER BY id DESC LIMIT 10000"
            ).fetchall()
            logs = [tuple(str(value) for value in row) for row in reversed(rows)]
        except sqlite3.DatabaseError:
            pass
    finally:
        if source is not None:
            source.close()

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(f"{path.stem}.corrupt-{stamp}{path.suffix}")
    counter = 1
    while backup.exists():
        backup = path.with_name(f"{path.stem}.corrupt-{stamp}-{counter}{path.suffix}")
        counter += 1
    path.replace(backup)
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            sidecar.replace(backup.with_name(backup.name + suffix))
    return settings, logs, backup


def _geo_distance_values(
    static_lat_value: object,
    static_lng_value: object,
    dynamic_lat_value: object,
    dynamic_lng_value: object,
) -> float | None:
    """Проверяет диапазоны координат и вычисляет расстояние в километрах."""
    static_lat = float(static_lat_value)
    static_lng = float(static_lng_value)
    dynamic_lat = float(dynamic_lat_value)
    dynamic_lng = float(dynamic_lng_value)
    if not (-90 <= static_lat <= 90 and -90 <= dynamic_lat <= 90):
        return None
    if not (-180 <= static_lng <= 180 and -180 <= dynamic_lng <= 180):
        return None
    return haversine_km(static_lat, static_lng, dynamic_lat, dynamic_lng)


def _sqlite_haversine_km(
    static_lat: object,
    static_lng: object,
    dynamic_lat: object,
    dynamic_lng: object,
) -> float | None:
    """Адаптирует безопасный haversine к протоколу пользовательской функции SQLite."""
    if any(
        value is None for value in (static_lat, static_lng, dynamic_lat, dynamic_lng)
    ):
        return None
    try:
        return _geo_distance_values(static_lat, static_lng, dynamic_lat, dynamic_lng)
    except TypeError, ValueError:
        return None


def _normalize_source(value: str) -> str:
    """Ограничивает источник истории значениями ``real`` и ``fake``."""
    return "fake" if str(value).strip().lower() == "fake" else "real"


def _demo_xy(
    lat: float, lng: float, center_lat: float, center_lng: float
) -> tuple[float, float]:
    """Строит условные экранные координаты точки для демонстрационной истории."""
    return (
        round(500 + (lng - center_lng) * 45, 2),
        round(400 - (lat - center_lat) * 70, 2),
    )
