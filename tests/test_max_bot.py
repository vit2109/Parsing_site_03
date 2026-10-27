from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from parsing_site.browser import NumberedCapture
from parsing_site.max_bot import (
    MaxAlertEngine,
    MaxDelivery,
    MaxDispatch,
    _ssl_context,
    send_max_message,
)
from parsing_site.settings_store import (
    _max_token_from_dotenv,
    from_dict,
    resolve_max_token,
    validate_settings,
)
from parsing_site.tracking import DistanceRow, SceneObject, TrackingSnapshot
from parsing_site.worker import ParseWorker


def settings():
    return validate_settings(from_dict({
        "max_enabled": True, "max_token": "secret-token",
        "static_objects": [
            {"id": 1, "name": "Пост 1", "x": 1, "y": 1},
            {"id": 2, "name": "Пост 2", "x": 2, "y": 2},
        ],
        "max_recipients": [
            {"user_id": 111, "rules": [
                {"static_index": 1, "zones": ["blue", "warning", "alert"]},
                {"static_index": 2, "zones": ["alert"]},
            ]},
            {"user_id": 222, "rules": [
                {"static_index": 1, "zones": ["alert"]},
            ]},
        ],
    }))


def snapshot(status: str) -> TrackingSnapshot:
    def row(static_id: int, distance: float, zone: str) -> DistanceRow:
        return DistanceRow(static_id, f"Пост {static_id}", 0, 0,
                           87, "Автомобиль", 0, 0, "", distance, zone, "km")

    return TrackingSnapshot("", "", (100, 100),
                            [SceneObject(87, "Автомобиль", 0, 0)], [],
                            [row(1, 5, status), row(2, 2, "ok")], 1, 3)


class MaxBotTest(unittest.TestCase):
    def test_first_poll_sends_existing_blue_object_once(self) -> None:
        engine = MaxAlertEngine()
        config = settings()
        self.assertEqual([item.user_id for item in engine.collect(snapshot("blue"), config, [])], [111])
        self.assertEqual(engine.collect(snapshot("blue"), config, []), [])

    def test_token_from_environment_without_database_copy(self) -> None:
        payload = settings().to_dict()
        payload["max_token"] = ""
        with patch.dict(os.environ, {"MAX_BOT_TOKEN": "env-secret"}):
            config = validate_settings(from_dict(payload))
            self.assertEqual(config.max_token, "")
            self.assertTrue(config.public_dict()["max_token_set"])
            self.assertEqual(resolve_max_token(config), "env-secret")

    def test_token_from_local_dotenv(self) -> None:
        with TemporaryDirectory() as directory:
            Path(directory, ".env").write_text("MAX_BOT_TOKEN=file-secret\n", encoding="utf-8")
            with patch.dict(os.environ, {"MAX_BOT_TOKEN": ""}), \
                    patch("parsing_site.settings_store.ROOT", Path(directory)):
                _max_token_from_dotenv.cache_clear()
                self.assertEqual(resolve_max_token(from_dict({})), "file-secret")
        _max_token_from_dotenv.cache_clear()

    def test_tls_uses_local_ca_bundle(self) -> None:
        with TemporaryDirectory() as directory:
            bundle = Path(directory, "certs", "ca-bundle.pem")
            bundle.parent.mkdir()
            bundle.write_text("test", encoding="utf-8")
            with patch.dict(os.environ, {"SSL_CERT_FILE": "", "SSL_CERT_DIR": ""}), \
                    patch("parsing_site.max_bot.ROOT", Path(directory)), \
                    patch("parsing_site.max_bot.ssl.create_default_context") as create:
                _ssl_context.cache_clear()
                _ssl_context()
                create.assert_called_once_with(cafile=str(bundle))
        _ssl_context.cache_clear()

    def test_settings_validate_and_hide_token(self) -> None:
        config = settings()
        self.assertEqual(config.public_dict()["max_token"], "")
        self.assertTrue(config.public_dict()["max_token_set"])
        self.assertEqual(from_dict(config.to_dict()).max_recipients[0].user_id, 111)
        bad = config.to_dict()
        bad["max_groups"][0]["rules"][0]["static_index"] = 999
        with self.assertRaises(ValueError):
            validate_settings(from_dict(bad))

    def test_group_parses_several_users_and_all_objects(self) -> None:
        payload = settings().to_dict()
        payload["max_groups"] = [{
            "user_ids": "111, 222 333",
            "all_objects": True,
            "zones": ["blue", "alert"],
            "rules": [],
        }]
        config = validate_settings(from_dict(payload))
        self.assertEqual([item.user_id for item in config.max_recipients], [111, 222, 333])
        self.assertEqual(len(config.max_recipients[0].rules), 2)
        self.assertEqual(config.max_groups[0].user_ids, [111, 222, 333])
        self.assertTrue(config.max_groups[0].all_objects)
        restored = validate_settings(from_dict(config.to_dict()))
        self.assertEqual(restored.max_groups[0].user_ids, [111, 222, 333])

    def test_old_recipient_settings_migrate_to_groups(self) -> None:
        legacy = settings().to_dict()
        del legacy["max_groups"]
        restored = validate_settings(from_dict(legacy))
        self.assertEqual([group.user_ids for group in restored.max_groups], [[111], [222]])
        self.assertEqual(len(restored.max_recipients), 2)

    def test_group_rejects_duplicate_object_or_user(self) -> None:
        payload = settings().to_dict()
        payload["max_groups"] = [{
            "user_ids": "111, 111",
            "rules": [{"static_index": 1, "zones": ["blue"]}],
        }]
        with self.assertRaises(ValueError):
            validate_settings(from_dict(payload))
        payload["max_groups"][0]["user_ids"] = "111, 222"
        payload["max_groups"][0]["rules"].append(
            {"static_index": 1, "zones": ["alert"]},
        )
        with self.assertRaises(ValueError):
            validate_settings(from_dict(payload))

    def test_same_user_in_two_groups_combines_rules_once(self) -> None:
        payload = settings().to_dict()
        payload["max_groups"] = [
            {"user_ids": "111", "rules": [{"static_index": 1, "zones": ["blue"]}]},
            {"user_ids": "111, 222", "rules": [
                {"static_index": 1, "zones": ["alert"]},
                {"static_index": 2, "zones": ["warning"]},
            ]},
        ]
        config = validate_settings(from_dict(payload))
        self.assertEqual([item.user_id for item in config.max_recipients], [111, 222])
        self.assertEqual(config.max_recipients[0].rules[0].zones, ["blue", "alert"])

    def test_entry_transitions_recipients_and_matching_screenshot(self) -> None:
        config = settings()
        engine = MaxAlertEngine()
        with TemporaryDirectory() as directory:
            old = Path(directory) / "old.jpg"
            match = Path(directory) / "match.jpg"
            unrelated = Path(directory) / "unrelated.jpg"
            old.write_bytes(b"x")
            match.write_bytes(b"x")
            unrelated.write_bytes(b"x")
            now = datetime.now().astimezone()
            photos = [(old, now - timedelta(minutes=3), (87,)),
                      (match, now - timedelta(seconds=2), (87, 99)),
                      (unrelated, now, (99,))]
            first = engine.collect(snapshot("blue"), config, photos)
            self.assertEqual([item.user_id for item in first], [111])
            self.assertEqual(first[0].screenshot, match)
            self.assertIn("Ближайший объект: «Пост 2», 2.0 км", first[0].text)
            self.assertEqual(engine.collect(snapshot("blue"), config, []), [])
            second = engine.collect(snapshot("alert"), config, [])
            self.assertEqual([item.user_id for item in second], [111, 222])
            self.assertEqual(engine.collect(snapshot("ok"), config, []), [])
            self.assertEqual(len(engine.collect(snapshot("blue"), config, [])), 1)

    def test_send_image_uses_upload_then_message(self) -> None:
        with TemporaryDirectory() as directory:
            photo = Path(directory) / "map.jpg"
            photo.write_bytes(b"jpeg")
            calls = []

            def request(url, token, body=None, content_type="application/json"):
                calls.append((url, token, body, content_type))
                if "/uploads?" in url:
                    return {"url": "https://upload.example/image"}
                if url.startswith("https://upload.example"):
                    return {"photos": {"photo-id": {"token": "image-token"}}}
                return {"message": {}}

            with patch("parsing_site.max_bot._request_json", side_effect=request):
                send_max_message(MaxDelivery(111, "secret", "Текст", photo))
            self.assertEqual(len(calls), 3)
            self.assertTrue(calls[1][2].find(b"jpeg") >= 0)
            body = json.loads(calls[2][2])
            self.assertEqual(body["attachments"][0]["payload"], {"token": "image-token"})
            self.assertEqual(calls[2][0].split("?user_id=")[1], "111")

    def test_worker_captures_for_max_even_when_archive_disabled(self) -> None:
        with TemporaryDirectory() as directory:
            config = settings()
            self.assertFalse(config.screenshot_enabled)
            database = Mock()
            database.save_snapshot.return_value = 1
            store = Mock()
            store.current.return_value = config
            worker = ParseWorker(database, store, Mock())
            worker._max_dispatch.submit = Mock()

            class Session:
                numbered_capture_error = ""
                numbered_html_error = ""

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def prepare_numbered_capture(self, size, html_enabled, on_capture):
                    self.size = size
                    self.on_capture = on_capture

            session = Session()

            def capture(_session, _tracking):
                session.on_capture(NumberedCapture(
                    datetime.now().astimezone(), 7, b"jpeg", numbers=(87,),
                ))
                worker._stop.set()
                return snapshot("blue")

            with patch("parsing_site.worker.BrowserSession", return_value=session), \
                    patch("parsing_site.worker.capture_snapshot", side_effect=capture), \
                    patch("parsing_site.capture_archive.ROOT", Path(directory)):
                worker._run()
            self.assertEqual(session.size, (config.screenshot_width, config.screenshot_height))
            delivery = worker._max_dispatch.submit.call_args.args[0]
            self.assertEqual(delivery.user_id, 111)
            self.assertTrue(delivery.screenshot.is_file())

    def test_disabling_max_skips_waiting_notifications(self) -> None:
        config = settings()
        config.max_enabled = False
        dispatch = MaxDispatch(Mock(), lambda: config)
        with patch("parsing_site.max_bot.send_max_message") as sender:
            dispatch.submit(MaxDelivery(111, "old-token", "Текст", None))
            dispatch._queue.join()
        sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
