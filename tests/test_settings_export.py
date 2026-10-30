from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from parsing_site.db import Database
from parsing_site.settings_store import (
    SETTINGS_EXPORT_FORMAT,
    SettingsStore,
    from_dict,
)


class SettingsExportTest(unittest.TestCase):
    def test_new_database_imports_adjacent_json_once(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "data" / "tracking.db"
            export = root / "settings.json"
            settings = from_dict({
                "site": "http://example.test/map",
                "static_objects": [{"id": 17, "name": "Пост", "x": 0, "y": 0,
                                    "lat": 50.5, "lng": 36.5}],
                "scan_mode": "adaptive", "max_token": "portable-token",
            })
            export.write_text(json.dumps({
                "format": SETTINGS_EXPORT_FORMAT, "version": 1,
                "settings": settings.to_dict(),
            }, ensure_ascii=False), encoding="utf-8")
            db = Database(db_path)
            self.assertTrue(db.was_created)
            store = SettingsStore(db, bootstrap_path=export)
            loaded = store.ensure_defaults()
            self.assertEqual(loaded.site, "http://example.test/map")
            self.assertEqual(loaded.static_objects[0].id, 17)
            self.assertEqual(loaded.max_token, "portable-token")
            db.close()

            export.write_text("invalid", encoding="utf-8")
            db = Database(db_path)
            self.assertFalse(db.was_created)
            self.assertEqual(SettingsStore(db, bootstrap_path=export).ensure_defaults().site,
                             "http://example.test/map")
            db.close()

    def test_export_endpoint_contains_saved_settings_and_objects(self) -> None:
        from parsing_site import webapp

        settings = from_dict({
            "max_token": "portable-token",
            "static_objects": [{"id": 8, "name": "Точка", "x": 0, "y": 0,
                                "lat": 51, "lng": 37}],
        })
        with patch.object(webapp.store, "current", return_value=settings):
            response = webapp.api_export_settings()
        payload = json.loads(response.body)
        self.assertEqual(payload["format"], SETTINGS_EXPORT_FORMAT)
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["settings"]["max_token"], "portable-token")
        self.assertEqual(payload["settings"]["static_objects"][0]["id"], 8)
        self.assertIn("settings.json", response.headers["content-disposition"])
        settings.max_token = ""
        with patch.object(webapp.store, "current", return_value=settings), \
                patch("parsing_site.webapp.resolve_max_token", return_value="env-token"):
            response = webapp.api_export_settings()
        self.assertEqual(json.loads(response.body)["settings"]["max_token"], "env-token")


if __name__ == "__main__":
    unittest.main()
