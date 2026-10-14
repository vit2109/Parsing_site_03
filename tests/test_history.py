from __future__ import annotations

import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from parsing_site.config import TrackingConfig
from parsing_site.coordinates import MapView, container_to_latlng, latlng_to_container
from parsing_site.db import Database
from parsing_site.html_probe import extract_scene_from_html
from parsing_site.tracking import (
    SceneObject,
    TrackingSnapshot,
    build_distance_rows,
    build_tracking_snapshot,
)


class HistoryDatabaseTest(unittest.TestCase):
    def test_history_reads_do_not_wait_for_worker_connection_lock(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            with ThreadPoolExecutor(max_workers=1) as executor:
                db._lock.acquire()
                try:
                    result = executor.submit(db.list_history_statics, "real")
                    self.assertEqual(result.result(timeout=2), [])
                finally:
                    db._lock.release()
            db.close()

    def test_new_index_without_labels_uses_distinct_temporary_ids(self) -> None:
        path = Path(__file__).parent.parent / "html_test" / "index.html"
        if not path.exists():
            self.skipTest("пользовательский файл index.html не включён в репозиторий")
        html = path.read_text(encoding="utf-8")
        scene = extract_scene_from_html(
            html, TrackingConfig(dynamic_selector=".leaflet-marker-icon.map-incident")
        )
        self.assertGreater(len(scene["dynamics"]), 0)
        self.assertTrue(all(item["index"] < 0 for item in scene["dynamics"]))
        self.assertEqual(len({item["index"] for item in scene["dynamics"]}), len(scene["dynamics"]))
        self.assertGreater(scene["unnumbered_dynamics"], 0)

    def test_second_page_matches_numbers_by_marker_position(self) -> None:
        path = Path(__file__).parent.parent / "html_test" / "2.html"
        if not path.exists():
            self.skipTest("пользовательский файл 2.html не включён в репозиторий")
        scene = extract_scene_from_html(
            path.read_text(encoding="utf-8"),
            TrackingConfig(dynamic_selector=".leaflet-marker-icon.map-incident"),
        )
        self.assertEqual(
            {item["index"] for item in scene["dynamics"]},
            {801, 802, 803, 804, 805, 806, 807, 809, 810},
        )
        self.assertEqual(next(item["name"] for item in scene["dynamics"] if item["index"] == 805), "МВШ")

    def test_distance_table_uses_object_type_instead_of_generic_name(self) -> None:
        rows = build_distance_rows(
            [SceneObject(index=1, name="Точка", x=0, y=0)],
            [SceneObject(index=805, name="Объект", object_type="МВШ", x=1, y=1)],
            TrackingConfig(coordinate_mode="screen"),
            mode="screen",
        )
        self.assertEqual(rows[0].dynamic_name, "МВШ")

    def test_duplicate_markers_with_one_visible_number_are_one_object(self) -> None:
        html = """
        <div class="leaflet-marker-icon map-incident map-incident_aircraft-drone" style="transform: translate3d(4px, 8px, 0px)"></div>
        <div class="leaflet-marker-icon map-incident map-incident_aircraft-drone" style="transform: translate3d(4px, 8px, 0px)"></div>
        <div class="leaflet-marker-icon map-incident-operator-number" style="transform: translate3d(4px, 8px, 0px)">722</div>
        """
        scene = extract_scene_from_html(
            html, TrackingConfig(dynamic_selector=".leaflet-marker-icon.map-incident")
        )
        self.assertEqual([item["index"] for item in scene["dynamics"]], [722])

    def test_retention_keeps_recent_real_and_all_fake_polls(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            db.ensure_fake_history()
            snapshot = TrackingSnapshot(
                url="test", title="test", viewport=(1, 1), dynamics=[],
                statics=[], rows=[], threshold=1, warning_limit=2,
            )
            old_id = db.save_snapshot(snapshot)
            recent_id = db.save_snapshot(snapshot)
            old_day = (date.today() - timedelta(days=20)).isoformat()
            db._conn.execute("UPDATE polls SET ts = ? WHERE id = ?", (old_day, old_id))
            db._conn.commit()
            result = db.prune_dynamic_history()
            self.assertEqual(result["polls_deleted"], 1)
            self.assertIsNone(db._conn.execute("SELECT id FROM polls WHERE id = ?", (old_id,)).fetchone())
            self.assertIsNotNone(db._conn.execute("SELECT id FROM polls WHERE id = ?", (recent_id,)).fetchone())
            self.assertEqual(db._conn.execute("SELECT COUNT(*) FROM polls WHERE data_source = 'fake'").fetchone()[0], 169)
            db.close()

    def test_saved_page_uses_positioned_daily_object_numbers(self) -> None:
        path = Path(__file__).parent.parent / "html_test" / "ip.html"
        if not path.exists():
            self.skipTest("исторический HTML-снимок ip.html отсутствует")
        html = path.read_text(
            encoding="utf-8"
        )
        scene = extract_scene_from_html(
            html,
            TrackingConfig(dynamic_selector=".leaflet-marker-icon.map-incident"),
        )

        self.assertEqual(
            [(item["index"], item["name"]) for item in scene["dynamics"]],
            [
                (61, "БПЛА"),
                (62, "БПЛА"),
                (63, "БПЛА"),
                (64, "БПЛА"),
                (66, "БПЛА"),
                (65, "БПЛА"),
            ],
        )

    def test_dynamic_object_types_follow_numbered_markers_not_card_order(self) -> None:
        html = """
        <div class="_card_a"><span class="_operatorNumber_a">1</span>БПЛА №1</div>
        <div class="_card_b"><span class="_operatorNumber_b">2</span>МВШ №2</div>
        <div class="_card_c"><span class="_operatorNumber_c">3</span>Ракета №3</div>
        <div class="leaflet-marker-icon map-incident map-incident_aircraft-drone" style="transform: translate3d(1px, 2px, 0px)"></div>
        <div class="leaflet-marker-icon map-incident map-incident_aircraft-balloon" style="transform: translate3d(3px, 4px, 0px)"></div>
        <div class="leaflet-marker-icon map-incident map-incident_rocket" style="transform: translate3d(5px, 6px, 0px)"></div>
        <div class="leaflet-marker-icon map-incident-operator-number" style="transform: translate3d(1px, 2px, 0px)">1</div>
        <div class="leaflet-marker-icon map-incident-operator-number" style="transform: translate3d(3px, 4px, 0px)">2</div>
        <div class="leaflet-marker-icon map-incident-operator-number" style="transform: translate3d(5px, 6px, 0px)">3</div>
        """
        scene = extract_scene_from_html(
            html,
            TrackingConfig(dynamic_selector=".leaflet-marker-icon.map-incident"),
        )

        self.assertEqual(
            [(item["index"], item["name"]) for item in scene["dynamics"]],
            [(1, "БПЛА"), (2, "МВШ"), (3, "Ракета")],
        )

    def test_configured_blue_distance_controls_classification(self) -> None:
        with patch("parsing_site.tracking.get_phone_for_static", return_value=""):
            snapshot = build_tracking_snapshot(
                url="https://example.test/map",
                title="Карта",
                tracking=TrackingConfig(
                    coordinate_mode="latlng",
                    static_source="page",
                    threshold=50,
                    warning_distance=150,
                    blue_distance=275,
                ),
                raw={
                    "coordinate_mode": "latlng",
                    "dynamics": [
                        {
                            "index": 7,
                            "name": "Объект №7",
                            "x": 0,
                            "y": 0,
                            "lat": 57.8,
                            "lng": 37.6173,
                        }
                    ],
                    "statics": [
                        {
                            "index": 1,
                            "name": "Москва",
                            "x": 0,
                            "y": 0,
                            "lat": 55.7558,
                            "lng": 37.6173,
                        }
                    ],
                },
            )

        self.assertEqual(snapshot.rows[0].status, "blue")
        self.assertEqual(snapshot.blue_limit, 275.0)

    def test_fake_history_is_complete_and_isolated(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            counts = db.ensure_fake_history()
            self.assertEqual(counts, {"polls": 169, "dynamics": 1014, "statics": 507})
            self.assertEqual(db.list_history_statics("real"), [])
            fake_statics = db.list_history_statics("fake")
            self.assertEqual(len(fake_statics), 3)
            poll = db._conn.execute(
                "SELECT * FROM polls WHERE data_source = 'fake' LIMIT 1"
            ).fetchone()
            for field in (
                "ts",
                "url",
                "title",
                "coordinate_mode",
                "distance_unit",
                "threshold",
                "warning_limit",
                "map_width",
                "map_height",
                "map_zoom",
                "map_center_lat",
                "map_center_lng",
                "geo_source",
            ):
                self.assertIsNotNone(poll[field], field)
            observation = db._conn.execute(
                "SELECT * FROM dynamic_observations LIMIT 1"
            ).fetchone()
            for field in (
                "x",
                "y",
                "lat",
                "lng",
                "dx",
                "dy",
                "distance_moved",
                "color",
            ):
                self.assertIsNotNone(observation[field], field)
            rows = db.list_distance_history(
                static_index=1,
                date_from=fake_statics[0]["first_seen"],
                date_to=fake_statics[0]["last_seen"],
                max_distance_km=500,
                data_source="fake",
            )
            self.assertGreater(len(rows), 0)
            self.assertTrue(all(row["distance_unit"] == "km" for row in rows))
            trajectory = db.list_dynamic_trajectory(
                dynamic_index=1,
                date_from=fake_statics[0]["first_seen"],
                date_to=fake_statics[0]["last_seen"],
                data_source="fake",
            )
            self.assertEqual(len(trajectory), 169)
            self.assertLessEqual(trajectory[0]["ts"], trajectory[-1]["ts"])
            zones = db.get_static_zone_trajectories(
                static_index=1,
                date_from=fake_statics[0]["first_seen"],
                date_to=fake_statics[0]["last_seen"],
                data_source="fake",
            )
            self.assertIsNotNone(zones)
            self.assertEqual(zones["alert_radius_km"], 50.0)
            self.assertEqual(zones["warning_radius_km"], 150.0)
            self.assertEqual(zones["blue_radius_km"], 350.0)
            self.assertGreater(len(zones["points"]), 0)
            self.assertTrue(
                any(
                    point["status"] in {"alert", "warning", "blue"}
                    for point in zones["points"]
                )
            )
            db.close()

    def test_daily_object_number_builds_separate_trajectories(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            static = SceneObject(
                index=1, name="Москва", x=0, y=0, lat=55.7558, lng=37.6173
            )

            def save_point(lat: float, lng: float, timestamp: str) -> int:
                dynamic = SceneObject(
                    index=62,
                    name="БПЛА",
                    x=0,
                    y=0,
                    lat=lat,
                    lng=lng,
                )
                poll_id = db.save_snapshot(
                    TrackingSnapshot(
                        url="https://example.test/map",
                        title="Карта",
                        viewport=(1000, 800),
                        dynamics=[dynamic],
                        statics=[static],
                        rows=[],
                        threshold=50,
                        warning_limit=150,
                        blue_limit=350,
                        coordinate_mode="latlng",
                        distance_unit="km",
                    )
                )
                db._conn.execute(
                    "UPDATE polls SET ts = ? WHERE id = ?", (timestamp, poll_id)
                )
                db._conn.commit()
                return poll_id

            save_point(55.76, 37.62, "2026-10-12T10:00:00+03:00")
            save_point(55.77, 37.63, "2026-10-12T11:00:00+03:00")
            save_point(56.00, 37.90, "2026-10-13T10:00:00+03:00")

            first_day = db.list_dynamic_trajectory(
                dynamic_index=62,
                object_date="2026-10-12",
                date_from="2026-10-12T00:00:00",
                date_to="2026-10-13T23:59:59",
            )
            second_day = db.list_dynamic_trajectory(
                dynamic_index=62,
                object_date="2026-10-13",
                date_from="2026-10-12T00:00:00",
                date_to="2026-10-13T23:59:59",
            )

            self.assertEqual(len(first_day), 2)
            self.assertEqual(len(second_day), 1)
            self.assertTrue(
                all(point["object_date"] == "2026-10-12" for point in first_day)
            )
            self.assertEqual(second_day[0]["object_date"], "2026-10-13")

            zones = db.get_static_zone_trajectories(
                static_index=1,
                date_from="2026-10-12T00:00:00",
                date_to="2026-10-13T23:59:59",
            )
            self.assertIsNotNone(zones)
            identities = {
                (point["object_date"], point["dynamic_index"])
                for point in zones["points"]
            }
            self.assertEqual(
                identities, {("2026-10-12", 62), ("2026-10-13", 62)}
            )
            db.close()

    def test_screen_snapshot_converts_pixels_before_history_save(self) -> None:
        with patch("parsing_site.tracking.get_phone_for_static", return_value=""):
            snapshot = build_tracking_snapshot(
                url="https://example.test/map",
                title="Карта",
                tracking=TrackingConfig(
                    coordinate_mode="screen", static_source="page", threshold=100
                ),
                raw={
                    "coordinate_mode": "screen",
                    "viewport": {"width": 1000, "height": 800},
                    "map": {
                        "width": 1000,
                        "height": 800,
                        "zoom": 10,
                        "center": {"lat": 55.7558, "lng": 37.6173},
                        "geo_source": "leaflet",
                    },
                    "dynamics": [{"index": 1, "name": "Машина", "x": 520, "y": 400}],
                    "statics": [{"index": 1, "name": "Склад", "x": 500, "y": 400}],
                },
            )
        self.assertEqual(snapshot.distance_unit, "px")
        self.assertIsNotNone(snapshot.rows[0].static_lat)
        self.assertIsNotNone(snapshot.rows[0].dynamic_lat)
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            poll_id = db.save_snapshot(snapshot)
            stored = db._conn.execute(
                "SELECT s.lat, s.lng, d.lat, d.lng FROM static_observations s "
                "JOIN dynamic_observations d ON d.poll_id = s.poll_id "
                "WHERE s.poll_id = ?",
                (poll_id,),
            ).fetchone()
            self.assertTrue(all(value is not None for value in stored))
            db.close()

    def test_map_center_converts_pixels_to_geographic_coordinates(self) -> None:
        view = MapView(
            width=1000,
            height=800,
            zoom=10,
            center_lat=55.7558,
            center_lng=37.6173,
        )
        center = container_to_latlng(500, 400, view)
        self.assertIsNotNone(center)
        self.assertAlmostEqual(center[0], 55.7558, places=5)
        self.assertAlmostEqual(center[1], 37.6173, places=5)
        projected = latlng_to_container(center[0], center[1], view)
        self.assertIsNotNone(projected)
        self.assertAlmostEqual(projected[0], 500, places=5)
        self.assertAlmostEqual(projected[1], 400, places=5)

    def test_migrates_old_database_and_filters_kilometres(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tracking.db"
            self._create_old_database(path)

            db = Database(path)
            tables = {
                row[0]
                for row in db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertNotIn("distance_rows", tables)
            self.assertNotIn("movements", tables)
            self.assertEqual(
                db._conn.execute("SELECT COUNT(*) FROM static_observations").fetchone()[
                    0
                ],
                2,
            )
            self.assertEqual(
                db._conn.execute(
                    "SELECT COUNT(*) FROM dynamic_observations"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(db.get_setting("storage_schema"), "2")
            self.assertEqual(db.get_setting("legacy_dynamic_cleanup_v1"), "1")
            runtime = json.loads(db.get_setting("runtime") or "{}")
            self.assertEqual(runtime["static_objects"][0]["name"], "Склад")

            static = SceneObject(
                index=1, name="Склад", x=0, y=0, lat=55.7558, lng=37.6173
            )
            dynamic = SceneObject(
                index=7, name="Машина", x=0, y=0, lat=55.76, lng=37.62
            )
            db.save_snapshot(
                TrackingSnapshot(
                    url="https://example.test/map",
                    title="Карта",
                    viewport=(1000, 800),
                    dynamics=[dynamic],
                    statics=[static],
                    rows=[],
                    threshold=1,
                    warning_limit=2,
                    coordinate_mode="latlng",
                    distance_unit="km",
                )
            )
            stored_ts = db._conn.execute(
                "SELECT ts FROM polls ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
            day = stored_ts[:10]

            rows = db.list_distance_history(
                static_index=1,
                date_from=f"{day}T00:00:00",
                date_to=f"{day}T23:59:59",
                max_distance_km=1,
                dynamic_query="Маш",
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["dynamic_name"], "Машина")
            self.assertEqual(rows[0]["distance_unit"], "km")
            self.assertEqual(
                db.list_distance_history(
                    static_index=1,
                    date_from=f"{day}T00:00:00",
                    date_to=f"{day}T23:59:59",
                    max_distance_km=0.1,
                ),
                [],
            )
            db.close()

    def test_recovers_from_malformed_database_instead_of_crashing(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "tracking.db"
            path.write_bytes(b"this is not sqlite")

            db = Database(path)

            self.assertIsNotNone(db.recovery_notice)
            self.assertEqual(db.get_setting("storage_schema"), "2")
            self.assertEqual(db._conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            backups = list(Path(directory).glob("tracking.corrupt-*.db"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"this is not sqlite")
            db.close()

    def test_pixel_row_with_geo_is_available_as_kilometres(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            static = SceneObject(
                index=1, name="Склад", x=100, y=100, lat=55.7558, lng=37.6173
            )
            dynamic = SceneObject(
                index=2, name="Машина", x=500, y=500, lat=55.76, lng=37.62
            )
            db.save_snapshot(
                TrackingSnapshot(
                    url="https://example.test/map",
                    title="Карта",
                    viewport=(1000, 800),
                    dynamics=[dynamic],
                    statics=[static],
                    rows=[],
                    threshold=100,
                    warning_limit=200,
                    coordinate_mode="screen",
                    distance_unit="px",
                )
            )
            stored = db._conn.execute(
                "SELECT ts, distance_unit FROM polls ORDER BY id DESC LIMIT 1"
            ).fetchone()
            self.assertEqual(stored[1], "px")
            day = stored[0][:10]
            rows = db.list_distance_history(
                static_index=1,
                date_from=f"{day}T00:00:00",
                date_to=f"{day}T23:59:59",
                max_distance_km=1,
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["distance_unit"], "km")
            self.assertLess(rows[0]["distance"], 1)
            db.close()

    @staticmethod
    def _create_old_database(path: Path) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                url TEXT,
                title TEXT,
                dynamics_count INTEGER NOT NULL DEFAULT 0,
                statics_count INTEGER NOT NULL DEFAULT 0,
                alerts INTEGER NOT NULL DEFAULT 0,
                warnings INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE distance_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                poll_id INTEGER,
                static_index INTEGER NOT NULL,
                static_name TEXT NOT NULL,
                static_x REAL NOT NULL,
                static_y REAL NOT NULL,
                dynamic_index INTEGER NOT NULL,
                dynamic_name TEXT NOT NULL,
                dynamic_x REAL NOT NULL,
                dynamic_y REAL NOT NULL,
                distance REAL NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO polls(id, ts, dynamics_count, statics_count)
            VALUES(1, '2026-01-01T12:00:00', 2, 2);
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO settings(key, value) VALUES(
                'runtime',
                '{"static_objects":[{"id":1,"name":"Склад","x":10,"y":20}]}'
            );
            INSERT INTO distance_rows(
                ts, poll_id, static_index, static_name, static_x, static_y,
                dynamic_index, dynamic_name, dynamic_x, dynamic_y, distance, status
            ) VALUES
                ('2026-01-01T12:00:00', 1, 1, 'S1', 10, 20, 1, 'D1', 30, 40, 28.28, 'alert'),
                ('2026-01-01T12:00:00', 1, 1, 'S1', 10, 20, 2, 'D2', 50, 60, 56.57, 'ok'),
                ('2026-01-01T12:00:00', 1, 2, 'S2', 15, 25, 1, 'D1', 30, 40, 21.21, 'alert'),
                ('2026-01-01T12:00:00', 1, 2, 'S2', 15, 25, 2, 'D2', 50, 60, 49.50, 'ok');
            """
        )
        connection.close()


if __name__ == "__main__":
    unittest.main()
