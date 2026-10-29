from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from parsing_site.browser import NumberedCapture
from parsing_site.capture_archive import ScreenshotRecord
from parsing_site.coordinates import MapView
from parsing_site.db import Database
from parsing_site.max_bot import (
    MaxAlertEngine,
    MaxDelivery,
    MaxDispatch,
    _request_json,
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
                           87, "Автомобиль", 0, 0, "", distance, zone, "km",
                           static_lat=0, static_lng=static_id - 1,
                           dynamic_lat=0, dynamic_lng=0)

    return TrackingSnapshot("", "", (100, 100),
                            [SceneObject(87, "Автомобиль", 0, 0, lat=0, lng=0)], [],
                            [row(1, 5, status), row(2, 2, "ok")], 1, 3)


class MaxBotTest(unittest.TestCase):
    def test_first_poll_sends_existing_blue_object_once(self) -> None:
        engine = MaxAlertEngine()
        config = settings()
        self.assertEqual([item.user_id for item in engine.collect(snapshot("blue"), config, [])], [111])
        self.assertEqual(engine.collect(snapshot("blue"), config, []), [])

    def test_entry_into_selected_zone_from_unselected_zone(self) -> None:
        payload = settings().to_dict()
        payload["max_groups"] = [{"user_ids": "111", "rules": [
            {"static_index": 1, "zones": ["blue"]},
        ]}]
        config = validate_settings(from_dict(payload))
        engine = MaxAlertEngine()
        self.assertEqual(engine.collect(snapshot("alert"), config, []), [])
        entered = engine.collect(snapshot("blue"), config, [])
        self.assertEqual(len(entered), 1)
        self.assertIn("синюю зону", entered[0].text)
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
            with patch("parsing_site.max_bot.ROOT", Path(directory)), \
                    patch("parsing_site.max_bot.ssl.create_default_context") as create:
                _ssl_context.cache_clear()
                _ssl_context()
                create.assert_called_once_with()
                create.return_value.load_verify_locations.assert_called_once_with(cafile=str(bundle))
        _ssl_context.cache_clear()

    def test_tls_uses_bundled_ca_after_exe_is_moved(self) -> None:
        with TemporaryDirectory() as directory:
            bundle = Path(directory, "bundle", "certs", "ca-bundle.pem")
            bundle.parent.mkdir(parents=True)
            bundle.write_text("test", encoding="utf-8")
            with patch("parsing_site.max_bot.ROOT", Path(directory, "installed")), \
                    patch("parsing_site.max_bot.BUNDLE_ROOT", Path(directory, "bundle")), \
                    patch("parsing_site.max_bot.ssl.create_default_context") as create:
                _ssl_context.cache_clear()
                _ssl_context()
                create.return_value.load_verify_locations.assert_called_once_with(cafile=str(bundle))
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
            photos = [
                ScreenshotRecord(old, now - timedelta(minutes=3), (87,),
                                 MapView(1000, 800, zoom=8, center_lat=0, center_lng=30)),
                ScreenshotRecord(match, now - timedelta(seconds=2), (87, 99),
                                 MapView(1000, 800, zoom=8, center_lat=0, center_lng=1)),
                ScreenshotRecord(unrelated, now, (99,),
                                 MapView(1000, 800, zoom=8, center_lat=0, center_lng=1)),
            ]
            first = engine.collect(snapshot("blue"), config, photos)
            self.assertEqual([item.user_id for item in first], [111])
            self.assertEqual(first[0].screenshot, match)
            self.assertIn("Ближайший объект: «Пост 2», 2.0 км", first[0].text)
            self.assertEqual(engine.collect(snapshot("blue"), config, []), [])
            second = engine.collect(snapshot("alert"), config, [])
            self.assertEqual([item.user_id for item in second], [111, 222])
            self.assertEqual(engine.collect(snapshot("ok"), config, []), [])
            self.assertEqual(len(engine.collect(snapshot("blue"), config, [])), 1)

    def test_no_screenshot_when_static_point_is_outside_frame(self) -> None:
        with TemporaryDirectory() as directory:
            wrong = Path(directory) / "wrong.jpg"
            wrong.write_bytes(b"jpeg")
            scene = snapshot("blue")
            photo = ScreenshotRecord(
                wrong, datetime.now().astimezone(), (87,),
                MapView(1000, 800, zoom=8, center_lat=0, center_lng=30),
            )
            message = MaxAlertEngine().collect(scene, settings(), [photo])[0]
            self.assertIsNone(message.screenshot)
            self.assertIn("ближайшей стационарной точкой не найден", message.text)

    def test_same_dynamic_entering_two_zones_sends_one_message_per_user(self) -> None:
        payload = settings().to_dict()
        payload["max_groups"] = [{"user_ids": "111", "rules": [
            {"static_index": 1, "zones": ["blue"]},
            {"static_index": 2, "zones": ["blue"]},
        ]}]
        config = validate_settings(from_dict(payload))
        scene = snapshot("blue")
        scene.rows[1].status = "blue"
        messages = MaxAlertEngine().collect(scene, config, [])
        self.assertEqual(len(messages), 1)
        self.assertIn("Пост 1", messages[0].text)
        self.assertIn("Пост 2", messages[0].text)
        self.assertIn("вошёл в синюю зону", messages[0].text)

    def test_rate_limit_retries_after_server_pause(self) -> None:
        limited = urllib.error.HTTPError(
            "https://platform-api2.max.ru/messages", 429, "Too Many Requests",
            {"Retry-After": "1.5"}, io.BytesIO(b'{"code":"rate.limit"}'),
        )
        with patch("parsing_site.max_bot.urllib.request.urlopen",
                   side_effect=[limited, io.BytesIO(b"{}")]) as opener, \
                patch("parsing_site.max_bot._ssl_context"), \
                patch("parsing_site.max_bot.time.sleep") as sleep:
            self.assertEqual(_request_json("https://platform-api2.max.ru/messages", "token"), {})
        self.assertEqual(opener.call_count, 2)
        sleep.assert_called_once_with(1.5)

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
                result = send_max_message(MaxDelivery(111, "secret", "Текст", photo))
            self.assertIsNone(result)
            self.assertEqual(len(calls), 3)
            self.assertTrue(calls[1][2].find(b"jpeg") >= 0)
            body = json.loads(calls[2][2])
            self.assertEqual(body["attachments"][0]["payload"], {"token": "image-token"})
            self.assertEqual(calls[2][0].split("?user_id=")[1], "111")

    def test_image_upload_error_still_delivers_text(self) -> None:
        with TemporaryDirectory() as directory:
            photo = Path(directory) / "map.jpg"
            photo.write_bytes(b"jpeg")
            messages = []

            def request(url, _token, body=None, _content_type="application/json"):
                if "/uploads?" in url:
                    raise RuntimeError("MAX HTTP 503: upload unavailable")
                messages.append(json.loads(body))
                return {"message": {}}

            with patch("parsing_site.max_bot._request_json", side_effect=request):
                result = send_max_message(MaxDelivery(111, "secret", "Зональное событие", photo))
            self.assertIn("upload unavailable", result)
            self.assertEqual(len(messages), 1)
            self.assertNotIn("attachments", messages[0])
            self.assertIn("Зональное событие", messages[0]["text"])
            self.assertIn("Скриншот не удалось", messages[0]["text"])

    def test_image_not_ready_retries_then_delivers_with_image(self) -> None:
        with TemporaryDirectory() as directory:
            photo = Path(directory) / "map.jpg"
            photo.write_bytes(b"jpeg")
            messages = []

            def request(url, _token, body=None, _content_type="application/json"):
                if "/uploads?" in url:
                    return {"url": "https://upload.example/image"}
                if url.startswith("https://upload.example"):
                    return {"photos": {"photo-id": {"token": "image-token"}}}
                messages.append(json.loads(body))
                if len(messages) == 1:
                    raise RuntimeError("MAX HTTP 400: attachment.not.ready")
                return {"message": {}}

            with patch("parsing_site.max_bot._request_json", side_effect=request), \
                    patch("parsing_site.max_bot.time.sleep") as sleep:
                result = send_max_message(MaxDelivery(111, "secret", "Событие", photo))
            self.assertIsNone(result)
            self.assertEqual(len(messages), 2)
            self.assertIn("attachments", messages[1])
            sleep.assert_called_once_with(1)

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
                    view=MapView(1000, 800, zoom=7, center_lat=0, center_lng=1),
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
            self.assertEqual(worker.status["max_poll"]["prepared"], 1)
            self.assertEqual(worker.status["max_poll"]["screenshots"], 1)
            self.assertEqual(worker.status["max_poll"]["eligible_pairs"], 1)

    def test_zone_poll_queues_and_sends_message_with_screenshot(self) -> None:
        with TemporaryDirectory() as directory:
            database = Database(Path(directory) / "tracking.db")
            store = Mock()
            store.current.return_value = settings()
            worker = ParseWorker(database, store, Mock())
            sent = []
            uploads = []

            class Session:
                numbered_capture_error = ""
                numbered_html_error = ""

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def prepare_numbered_capture(self, _size, html_enabled, on_capture):
                    self.on_capture = on_capture

                def capture_pair_view(self, number, dynamic, static, _tracking):
                    self.on_capture(NumberedCapture(
                        datetime.now().astimezone(), 7, b"pair-jpeg", numbers=(number,),
                        view=MapView(1000, 800, zoom=7, center_lat=0, center_lng=1),
                    ))
                    return True

                def annotate_static_point(self, image, view, point, name):
                    self.annotation = (image, view, point, name)
                    return b"annotated-jpeg"

            session = Session()

            def capture(_session, _tracking):
                session.on_capture(NumberedCapture(
                    datetime.now().astimezone(), 7, b"jpeg", numbers=(87,),
                    view=MapView(1000, 800, zoom=7, center_lat=0, center_lng=30),
                ))
                worker._stop.set()
                return snapshot("blue")

            def request(url, _token, body=None, _content_type="application/json"):
                if "/uploads?" in url:
                    return {"url": "https://upload.example/image"}
                if url.startswith("https://upload.example"):
                    uploads.append(body)
                    return {"photos": {"photo-id": {"token": "image-token"}}}
                sent.append(json.loads(body))
                return {"message": {}}

            with patch("parsing_site.worker.BrowserSession", return_value=session), \
                    patch("parsing_site.worker.capture_snapshot", side_effect=capture), \
                    patch("parsing_site.capture_archive.ROOT", Path(directory)), \
                    patch("parsing_site.max_bot._request_json", side_effect=request):
                worker._run()
                worker._max_dispatch._queue.join()
            self.assertEqual(len(sent), 1)
            self.assertIn(b"annotated-jpeg", uploads[0])
            self.assertEqual(session.annotation[0], b"pair-jpeg")
            self.assertEqual(session.annotation[3], "Пост 2")
            self.assertIn("Объект №87", sent[0]["text"])
            self.assertEqual(sent[0]["attachments"][0]["payload"], {"token": "image-token"})
            self.assertEqual(database.list_max_deliveries()["items"][0]["status"], "sent")
            self.assertEqual(worker.status["max_poll"]["prepared_with_screenshot"], 1)
            database.close()

    def test_many_alerts_publish_table_before_limited_extra_captures(self) -> None:
        database = Mock()
        database.save_snapshot.return_value = 1
        store = Mock()
        store.current.return_value = settings()
        worker = ParseWorker(database, store, Mock())
        worker._max_dispatch.submit = Mock()
        scene = snapshot("blue")
        scene.dynamics = [
            SceneObject(number, "Автомобиль", 0, 0, lat=0, lng=0)
            for number in range(100, 110)
        ]
        scene.rows = [
            DistanceRow(1, "Пост 1", 0, 0, number, "Автомобиль", 0, 0,
                        "", 5, "blue", "km", static_lat=0, static_lng=0,
                        dynamic_lat=0, dynamic_lng=0)
            for number in range(100, 110)
        ]

        class Session:
            numbered_capture_error = ""
            numbered_html_error = ""
            captures = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def prepare_numbered_capture(self, _size, html_enabled, on_capture):
                return None

            def capture_pair_view(self, *_args):
                self.captures += 1
                self_published = worker.status["snapshot"]
                assert self_published is not None and self_published["poll_id"] == 1
                return False

        session = Session()

        def capture(_session, _tracking):
            worker._stop.set()
            return scene

        with patch("parsing_site.worker.BrowserSession", return_value=session), \
                patch("parsing_site.worker.capture_snapshot", side_effect=capture):
            worker._run()
        self.assertEqual(session.captures, 6)
        self.assertEqual(worker._max_dispatch.submit.call_count, 10)
        self.assertEqual(worker.status["max_poll"]["prepared"], 10)

    def test_disabling_max_skips_waiting_notifications(self) -> None:
        config = settings()
        config.max_enabled = False
        dispatch = MaxDispatch(Mock(), lambda: config)
        with patch("parsing_site.max_bot.send_max_message") as sender:
            dispatch.submit(MaxDelivery(111, "old-token", "Текст", None))
            dispatch._queue.join()
        sender.assert_not_called()

    def test_delivery_log_records_sent_failed_and_skipped(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            config = settings()
            dispatch = MaxDispatch(db.add_log, lambda: config, db)
            with patch("parsing_site.max_bot.send_max_message") as sender:
                sent_id = dispatch.submit(MaxDelivery(111, "secret", "Объект №87", None))
                dispatch._queue.join()
                sender.side_effect = RuntimeError("MAX HTTP 403")
                error_id = dispatch.submit(MaxDelivery(111, "secret", "Ошибка", None))
                dispatch._queue.join()
                config.max_enabled = False
                skipped_id = dispatch.submit(MaxDelivery(111, "secret", "Пропуск", None))
                dispatch._queue.join()
            items = {item["id"]: item for item in db.list_max_deliveries()["items"]}
            self.assertEqual(items[sent_id]["status"], "sent")
            self.assertEqual(items[error_id]["status"], "error")
            self.assertIn("403", items[error_id]["error"])
            self.assertEqual(items[skipped_id]["status"], "skipped")
            self.assertEqual(db.list_max_deliveries(status="error")["total"], 1)
            self.assertEqual(db.list_max_deliveries(user_id=222)["total"], 0)
            self.assertEqual(db.list_max_deliveries(limit=1)["items"][0]["id"], skipped_id)
            self.assertEqual(db.list_max_deliveries(limit=1, offset=1)["items"][0]["id"], error_id)
            queued_id = db.add_max_delivery(111, "alert", "Осталось в очереди", None)
            self.assertEqual(db.mark_interrupted_max_deliveries(), 1)
            self.assertEqual(db.list_max_deliveries()["items"][0]["id"], queued_id)
            self.assertEqual(db.list_max_deliveries()["items"][0]["status"], "skipped")
            db.close()

    def test_delivery_log_records_photo_error_without_losing_text(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            dispatch = MaxDispatch(db.add_log, settings, db)
            with patch("parsing_site.max_bot.send_max_message", return_value="HTTP 503"):
                delivery_id = dispatch.submit(MaxDelivery(111, "secret", "Объект №87", None))
                dispatch._queue.join()
            item = next(item for item in db.list_max_deliveries()["items"] if item["id"] == delivery_id)
            self.assertEqual(item["status"], "sent")
            self.assertIn("Скриншот не отправлен", item["error"])
            db.close()

    def test_manual_message_uses_same_queue_without_active_alerts(self) -> None:
        with TemporaryDirectory() as directory:
            db = Database(Path(directory) / "tracking.db")
            config = settings()
            config.max_enabled = False
            dispatch = MaxDispatch(db.add_log, lambda: config, db)
            with patch("parsing_site.max_bot.send_max_message") as sender:
                delivery_id = dispatch.submit_test(6864254)
                dispatch._queue.join()
            sender.assert_called_once()
            self.assertEqual(sender.call_args.args[0].user_id, 6864254)
            item = db.list_max_deliveries()["items"][0]
            self.assertEqual(item["id"], delivery_id)
            self.assertEqual(item["kind"], "test")
            self.assertEqual(item["status"], "sent")
            self.assertNotIn("secret-token", item["message"])
            db.close()


if __name__ == "__main__":
    unittest.main()
