from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import launcher


class LauncherTest(unittest.TestCase):
    def test_single_mode_preserves_original_ports_and_no_instance(self) -> None:
        targets = launcher.build_targets(False, ("", "", "", ""))
        self.assertEqual(targets, [launcher.Target("Основная программа", 8082, 9222)])
        self.assertIsNone(targets[0].instance)

    def test_two_modes_get_independent_names_and_ports(self) -> None:
        first, second = launcher.build_targets(True, ("8082", "9222", "8083", "9223"))
        self.assertEqual((first.instance, second.instance), ("algorithm_a", "algorithm_b"))
        self.assertEqual((first.web_port, first.cdp_port), (8082, 9222))
        self.assertEqual((second.web_port, second.cdp_port), (8083, 9223))

    def test_duplicate_port_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "различаться"):
            launcher.build_targets(True, ("8082", "9222", "8083", "9222"))

    def test_frozen_launcher_uses_sibling_backend(self) -> None:
        with patch.object(launcher.sys, "frozen", True, create=True), \
                patch.object(launcher.sys, "executable", r"C:\Apps\ParsingSiteLauncher.exe"):
            self.assertEqual(launcher.default_backend(), Path(r"C:\Apps\ParsingSite.exe"))

    def test_browsers_start_before_two_web_programs(self) -> None:
        with TemporaryDirectory() as directory:
            backend = Path(directory) / "ParsingSite.exe"
            backend.touch()
            targets = launcher.build_targets(True, ("8082", "9222", "8083", "9223"))
            events: list[tuple[str, list[str]]] = []
            process = MagicMock()
            process.poll.return_value = None

            def browser_run(args: list[str], **_kwargs: object) -> MagicMock:
                events.append(("browser", args))
                return MagicMock(returncode=0)

            def web_start(args: list[str], **_kwargs: object) -> MagicMock:
                events.append(("web", args))
                return process

            with patch.object(launcher, "port_occupied", return_value=False), \
                    patch.object(launcher, "cdp_ready", return_value=True), \
                    patch.object(launcher, "web_ready", return_value=True), \
                    patch.object(launcher.subprocess, "run", side_effect=browser_run), \
                    patch.object(launcher.subprocess, "Popen", side_effect=web_start):
                launcher.start_targets(backend, targets, True, lambda _message: None)
            self.assertEqual([event for event, _ in events],
                             ["browser", "browser", "web", "web"])
            self.assertIn("--instance", events[2][1])
            self.assertIn("algorithm_a", events[2][1])
            self.assertIn("algorithm_b", events[3][1])


if __name__ == "__main__":
    unittest.main()
