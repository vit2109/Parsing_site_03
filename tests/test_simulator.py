"""Детерминированные сценарии тренажёра без отправки или записи данных."""

import unittest
from unittest.mock import patch

from parsing_site.config import ScanZoneConfig, StaticObjectConfig
from parsing_site.settings_store import MaxObjectRule, MaxRecipient, RuntimeSettings
from parsing_site.simulator import build_scenario


class SimulatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = RuntimeSettings(
            threshold=20, warning_distance=60, blue_distance=120,
            scan_max_zoom=10,
            static_objects=[
                StaticObjectConfig(1, "Санкт-Петербург", 0, 0, 59.94, 30.31),
                StaticObjectConfig(2, "Соседняя точка", 0, 0, 59.96, 30.35),
                StaticObjectConfig(3, "Москва", 0, 0, 55.75, 37.62),
            ],
        )

    def scenario(self, count=60, **overrides):
        arguments = dict(count=count, area="static", distribution="balanced", seed=42,
                         reveal_zoom=8, group_radius=10)
        arguments.update(overrides)
        return build_scenario(self.settings, None, **arguments)

    def test_reproducible_zones_and_grouped_route(self):
        first = self.scenario()
        second = self.scenario()
        self.assertEqual(first["objects"], second["objects"])
        self.assertEqual(sum(first["zone_counts"].values()), 60)
        self.assertGreater(first["zone_counts"]["alert"], 0)
        self.assertGreater(first["zone_counts"]["warning"], 0)
        self.assertGreater(first["zone_counts"]["blue"], 0)
        self.assertEqual(len(first["routes"]["static_points"]), 3)
        self.assertEqual(len(first["routes"]["grouped_static"]), 2)
        self.assertIn("Всего динамических объектов: 60", first["preview"][0]["text"])

    def test_large_scenario_respects_route_limit(self):
        result = self.scenario(count=1000)
        self.assertEqual(len(result["objects"]), 1000)
        self.assertLessEqual(len(result["routes"]["adaptive"]), 32)
        self.assertEqual(sum(result["zone_counts"].values()), 1000)
        complete = result["routes"]["full_coverage"]
        self.assertEqual(len(complete), 1000)
        by_id = {item["id"]: item for item in result["objects"]}
        self.assertEqual({step["target_id"] for step in complete}, set(by_id))
        for step in complete:
            target = by_id[step["target_id"]]
            self.assertEqual((step["lat"], step["lng"]), (target["lat"], target["lng"]))
            self.assertGreaterEqual(step["zoom"], target["reveal_zoom"])

    def test_complete_mode_reaches_numbers_above_normal_zoom(self):
        self.settings.scan_max_zoom = 6
        result = self.scenario(count=25, reveal_zoom=13)
        self.assertTrue(all(step["zoom"] >= 12 for step in result["routes"]["full_coverage"]))
        self.assertTrue(all(step["zoom"] == 6 for step in result["routes"]["adaptive"]))

    def test_preview_never_sends(self):
        self.settings.max_recipients = [MaxRecipient(123, [MaxObjectRule(1, ["alert"])])]
        with patch("parsing_site.max_bot.send_max_message", side_effect=AssertionError("sent")):
            result = self.scenario()
        self.assertTrue(result["preview"])
        self.assertTrue(all(item["user_id"] == 123 for item in result["preview"]))

    def test_recent_positions_and_saved_sectors(self):
        self.settings.scan_zones = [ScanZoneConfig("Петербург", 59.94, 30.31, 80)]
        recent = {"dynamics": [{"dynamic_name": "Тест", "lat": 60.0, "lng": 30.4}]}
        result = build_scenario(
            self.settings, recent, count=12, area="recent", distribution="close",
            seed=7, reveal_zoom=8, group_radius=50)
        self.assertIn("последнего живого опроса", result["source"])
        self.assertEqual(result["zones"][0]["name"], "Петербург")
        self.assertEqual(len(result["routes"]["zones"]), 1)
        self.assertTrue(all(item["region"] == "Тест" for item in result["objects"]))

    def test_threshold_order_is_checked(self):
        with self.assertRaisesRegex(ValueError, "пороги"):
            self.scenario(red_km=100, yellow_km=50, blue_km=120)


if __name__ == "__main__":
    unittest.main()
