from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from parsing_site.phone_loader import PhoneLoader
from parsing_site.settings_store import from_dict
from parsing_site.table_view import persist_snapshot
from parsing_site.tracking import DistanceRow, TrackingSnapshot


class ReviewFixesTest(unittest.TestCase):
    """Регрессии для дефектов, обнаруженных во время код-ревью."""

    def test_compact_csv_ignores_additional_api_fields(self) -> None:
        """Сериализатор API не должен ломать компактную CSV-выгрузку."""
        with tempfile.TemporaryDirectory() as temp_dir:
            path = persist_snapshot(_snapshot(), Path(temp_dir), refresh_seconds=2)

            self.assertTrue(path.exists())
            with (Path(temp_dir) / "distances.csv").open(
                encoding="utf-8", newline=""
            ) as source:
                rows = list(csv.DictReader(source))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["static_name"], "Пост")

    def test_phone_match_keeps_address_when_numbers_are_shared(self) -> None:
        """Одинаковый телефон не должен подменять исходный найденный адрес."""
        with tempfile.TemporaryDirectory() as temp_dir:
            book = Workbook()
            sheet = book.active
            sheet.append(("adres", "telephone"))
            sheet.append(("Улица Первая, 1", "+7 900 000-00-00"))
            sheet.append(("Улица Вторая, 2", "+7 900 000-00-00"))
            path = Path(temp_dir) / "phones.xlsx"
            book.save(path)

            loader = PhoneLoader(path)
            address, phone = loader.find_exact_match("улица вторая 2")

            self.assertEqual(address, "Улица Вторая, 2")
            self.assertEqual(phone, "+7 900 000-00-00")

    def test_sound_preference_round_trips_through_runtime_settings(self) -> None:
        """Отключённый звук должен сохраняться в JSON runtime-настроек."""
        settings = from_dict({"sound_enabled": False})

        self.assertFalse(settings.sound_enabled)
        self.assertFalse(settings.to_dict()["sound_enabled"])

    def test_blue_zone_settings_round_trip_and_keep_zone_order(self) -> None:
        """Расстояние и цвет синей зоны сохраняются, пороги не пересекаются."""
        settings = from_dict(
            {
                "threshold": 50,
                "warning_distance": 150,
                "blue_distance": 275,
                "blue_color": "#1267ab",
            }
        )

        self.assertEqual(settings.blue_distance, 275)
        self.assertEqual(settings.blue_color, "#1267ab")
        tracking = settings.to_app_config().tracking
        self.assertEqual(tracking.blue_limit, 275)
        self.assertEqual(tracking.blue_color, "#1267ab")


def _snapshot() -> TrackingSnapshot:
    """Создаёт минимальный снимок с одной строкой для файловой выгрузки."""
    row = DistanceRow(
        static_index=1,
        static_name="Пост",
        static_x=100,
        static_y=200,
        dynamic_index=1,
        dynamic_name="Объект",
        dynamic_x=110,
        dynamic_y=210,
        dynamic_color="#ff0000",
        distance=1.25,
        status="alert",
        distance_unit="km",
        static_lat=47.1,
        static_lng=39.1,
        dynamic_lat=47.2,
        dynamic_lng=39.2,
        alert_count=1,
        phone="+7 900 000-00-00",
    )
    return TrackingSnapshot(
        url="https://example.test/map",
        title="Карта",
        viewport=(1000, 800),
        dynamics=[],
        statics=[],
        rows=[row],
        threshold=1,
        warning_limit=2,
        coordinate_mode="latlng",
        distance_unit="km",
    )


if __name__ == "__main__":
    unittest.main()
