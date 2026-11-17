from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from parsing_site.maintenance import HistoryMaintenance
from parsing_site.settings_store import RuntimeSettings, SettingsStore, from_dict


class StartupTest(unittest.TestCase):
    def test_http_remains_responsive_while_background_cleanup_is_slow(self) -> None:
        import socket

        import uvicorn

        from parsing_site import webapp
        from parsing_site.db import Database
        from parsing_site.table_history_db import TableHistoryDatabase

        with TemporaryDirectory() as directory:
            database = Database(Path(directory) / "tracking.db")
            table = TableHistoryDatabase(Path(directory) / "table.db")
            store = SettingsStore(database)
            store.save(from_dict({}))
            worker = MagicMock()
            worker.status = {"running": False, "status": "stopped", "error": "", "polls": 0,
                             "revision": 0, "snapshot": None}
            entered, release = threading.Event(), threading.Event()

            def delayed_cleanup(*_args: object, **_kwargs: object) -> int:
                entered.set()
                release.wait(5)
                return 0

            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(webapp.app, host="127.0.0.1", port=port, log_level="error"))
            thread = threading.Thread(target=server.run, daemon=True)
            with patch.object(webapp, "db", database), patch.object(webapp, "table_history_db", table), \
                    patch.object(webapp, "store", store), patch.object(webapp, "worker", worker), \
                    patch.object(webapp.app.state, "uvicorn_server", None, create=True), \
                    patch.object(HistoryMaintenance, "_prune", side_effect=delayed_cleanup):
                thread.start()
                try:
                    deadline = time.monotonic() + 3
                    while not server.started and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(server.started, "HTTP startup waited for optional maintenance")
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(release.is_set())
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state?include_storage=false", timeout=2) as response:
                        self.assertEqual(response.status, 200)
                finally:
                    release.set()
                    server.should_exit = True
                    thread.join(timeout=8)
                self.assertFalse(thread.is_alive())

    def test_http_startup_does_not_wait_for_history_or_demo_work(self) -> None:
        from parsing_site import webapp

        fake_db, fake_table, fake_store, fake_worker = (MagicMock() for _ in range(4))
        fake_store.ensure_defaults.return_value = RuntimeSettings()
        fake_worker.status = {"running": False}
        fake_app = SimpleNamespace(state=SimpleNamespace())

        async def check() -> None:
            async with webapp.lifespan(fake_app):
                fake_db.prune_dynamic_history.assert_not_called()
                fake_table.prune_dynamic_history.assert_not_called()
                fake_db.ensure_fake_history.assert_not_called()
                fake_db.mark_interrupted_max_deliveries.assert_not_called()
                maintenance.start.assert_called_once()
                fake_db.close.assert_not_called()

        with patch.object(webapp, "db", fake_db), patch.object(webapp, "table_history_db", fake_table), \
                patch.object(webapp, "store", fake_store), patch.object(webapp, "worker", fake_worker), \
                patch.object(webapp, "HistoryMaintenance") as factory:
            maintenance = factory.return_value
            asyncio.run(check())
            maintenance.stop.assert_called_once()
            fake_db.close.assert_called_once()
            fake_table.close.assert_called_once()

    def test_fake_data_is_prepared_only_when_requested(self) -> None:
        from parsing_site import webapp

        with patch.object(webapp, "db") as database, patch.object(webapp, "store") as store:
            database.list_history_statics.return_value = []
            store.current.return_value = RuntimeSettings()
            webapp.api_history_statics(source="real")
            database.ensure_fake_history.assert_not_called()
            webapp.api_history_statics(source="fake")
            database.ensure_fake_history.assert_called_once()


class HistoryMaintenanceTest(unittest.TestCase):
    def test_cleanup_uses_batches_preserves_recent_and_fake_and_cascades(self) -> None:
        for table_history in (False, True):
            with self.subTest(table_history=table_history), TemporaryDirectory() as directory:
                path = Path(directory) / "history.db"
                parent, child, time_field, source_field, foreign_key = (
                    ("table_snapshots", "table_history", "captured_at", "source", "snapshot_id")
                    if table_history else ("polls", "observations", "ts", "data_source", "poll_id")
                )
                connection = sqlite3.connect(path)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(f"CREATE TABLE {parent}(id INTEGER PRIMARY KEY, {time_field} TEXT, {source_field} TEXT)")
                connection.execute(f"CREATE TABLE {child}(id INTEGER PRIMARY KEY, {foreign_key} INTEGER "
                                   f"REFERENCES {parent}(id) ON DELETE CASCADE)")
                today = datetime.now().astimezone().date()
                old = (today - timedelta(days=46)).isoformat()
                recent = (today - timedelta(days=30)).isoformat()
                for number in range(1, 14):
                    connection.execute(f"INSERT INTO {parent} VALUES(?, ?, ?)",
                                       (number, old if number <= 12 else recent, "real"))
                    connection.execute(f"INSERT INTO {child} VALUES(?, ?)", (number, number))
                connection.execute(f"INSERT INTO {parent} VALUES(14, ?, 'fake')", (old,))
                connection.commit()
                maintenance = HistoryMaintenance(path, path, lambda: RuntimeSettings(history_keep_days=45))
                # Открытое читательское соединение не мешает отдельному WAL-писателю.
                with patch.object(maintenance, "_execute", wraps=maintenance._execute) as execute:
                    count = maintenance._prune(path, table_history=table_history)
                self.assertEqual(count, 12)
                self.assertEqual(execute.call_count, 3)
                self.assertEqual(connection.execute(f"SELECT id FROM {parent} ORDER BY id").fetchall(), [(13,), (14,)])
                self.assertEqual(connection.execute(f"SELECT id FROM {child}").fetchall(), [(13,)])
                connection.close()

    def test_sql_deadline_aborts_and_rolls_back_long_operation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE items(value INTEGER)")
            connection.executemany("INSERT INTO items VALUES(?)", ((i,) for i in range(10000)))
            connection.commit()
            maintenance = HistoryMaintenance(path, path, RuntimeSettings)
            with patch("parsing_site.maintenance.monotonic", side_effect=[0, 10]), \
                    self.assertRaisesRegex(sqlite3.OperationalError, "interrupted"):
                maintenance._execute(path, "UPDATE items SET value=value+1", ())
            self.assertEqual(connection.execute("SELECT SUM(value) FROM items").fetchone()[0], 49995000)
            connection.close()

    def test_stop_prevents_any_initial_maintenance_and_exits_thread(self) -> None:
        maintenance = HistoryMaintenance(Path("unused.db"), Path("unused.db"), RuntimeSettings)
        maintenance._stop.set()
        with patch.object(maintenance, "_execute") as execute:
            maintenance.start()
            maintenance.stop()
        execute.assert_not_called()
        self.assertFalse(maintenance._thread.is_alive())

    def test_busy_database_does_not_block_maintenance_indefinitely(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE items(value INTEGER)")
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            maintenance = HistoryMaintenance(path, path, RuntimeSettings)
            finished = threading.Event()

            def run() -> None:
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                        maintenance._execute(path, "INSERT INTO items VALUES(1)", ())
                finally:
                    finished.set()

            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(finished.wait(2), "maintenance must have a finite lock wait")
            finally:
                connection.rollback()
                connection.close()
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
