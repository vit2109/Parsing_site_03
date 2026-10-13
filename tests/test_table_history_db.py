from __future__ import annotations

import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from parsing_site.table_history_db import TableHistoryDatabase
from parsing_site.tracking import DistanceRow, TrackingSnapshot


class TableHistoryDatabaseTest(unittest.TestCase):
    def test_retention_cascades_old_table_rows(self) -> None:
        with TemporaryDirectory() as directory:
            database = TableHistoryDatabase(Path(directory) / "table_history.db")
            old_day = (date.today() - timedelta(days=20)).isoformat()
            database.save_snapshot(_snapshot(rows=2), captured_at=old_day)
            database.save_snapshot(_snapshot(rows=1))
            result = database.prune_dynamic_history()
            self.assertEqual(result["snapshots_deleted"], 1)
            self.assertEqual(len(database.list_rows()), 1)
            database.close()

    def test_saves_every_visible_row_with_timestamp(self) -> None:
        with TemporaryDirectory() as directory:
            database = TableHistoryDatabase(Path(directory) / "table_history.db")
            snapshot = _snapshot(rows=2)

            snapshot_id = database.save_snapshot(
                snapshot,
                captured_at="2026-10-08T12:00:00+03:00",
            )

            rows = database.list_rows()
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row["snapshot_id"] == snapshot_id for row in rows))
            self.assertTrue(
                all(row["captured_at"] == "2026-10-08T12:00:00+03:00" for row in rows)
            )
            self.assertEqual(rows[0]["nearby_counts"], "кр. 1 · жёл. 1")
            self.assertEqual(rows[0]["phone"], "+7 900 000-00-00")
            database.close()

    def test_daily_estimate_uses_current_interval_and_row_count(self) -> None:
        with TemporaryDirectory() as directory:
            database = TableHistoryDatabase(Path(directory) / "table_history.db")
            database.save_snapshot(_snapshot(rows=2))

            stats = database.storage_stats(interval_seconds=2, current_rows=2)

            self.assertEqual(stats["snapshots_per_day"], 43_200)
            self.assertEqual(stats["estimated_rows_per_day"], 86_400)
            self.assertGreater(stats["estimated_bytes_per_day"], 0)
            self.assertEqual(stats["total_snapshots"], 1)
            self.assertEqual(stats["total_rows"], 2)
            database.close()

    def test_empty_snapshot_is_still_recorded(self) -> None:
        with TemporaryDirectory() as directory:
            database = TableHistoryDatabase(Path(directory) / "table_history.db")
            database.save_snapshot(_snapshot(rows=0))

            stats = database.storage_stats(interval_seconds=10)

            self.assertEqual(stats["total_snapshots"], 1)
            self.assertEqual(stats["total_rows"], 0)
            database.close()


def _snapshot(*, rows: int) -> TrackingSnapshot:
    table_rows = [
        DistanceRow(
            static_index=1,
            static_name="Пост",
            static_x=100,
            static_y=200,
            dynamic_index=index + 1,
            dynamic_name=f"Объект {index + 1}",
            dynamic_x=110 + index,
            dynamic_y=210 + index,
            dynamic_color="#ff0000",
            distance=1.25 + index,
            status="alert" if index == 0 else "warning",
            distance_unit="km",
            static_lat=47.1,
            static_lng=39.1,
            dynamic_lat=47.2 + index / 100,
            dynamic_lng=39.2 + index / 100,
            alert_count=1,
            warning_count=1,
            phone="+7 900 000-00-00",
        )
        for index in range(rows)
    ]
    return TrackingSnapshot(
        url="https://example.test/map",
        title="Карта",
        viewport=(1000, 800),
        dynamics=[],
        statics=[],
        rows=table_rows,
        threshold=1,
        warning_limit=2,
        coordinate_mode="latlng",
        distance_unit="km",
    )


if __name__ == "__main__":
    unittest.main()
