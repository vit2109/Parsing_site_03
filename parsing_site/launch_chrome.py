"""
Запуск Google Chrome / Chromium с remote-debugging-port для Playwright.

Профиль пишется в .chrome-profile, чтобы не трогать обычный Chrome пользователя.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from parsing_site.cdp_check import cdp_endpoint_ok
from parsing_site.config import ROOT

DEFAULT_PORT = 9222


def _chrome_candidates() -> list[Path]:
    """Возвращает стандартные пути установки Google Chrome в Windows."""
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return [
        Path(pf) / "Google/Chrome/Application/chrome.exe",
        Path(pfx86) / "Google/Chrome/Application/chrome.exe",
        Path(local) / "Google/Chrome/Application/chrome.exe",
    ]


def find_chrome() -> Path | None:
    """Находит первый существующий исполняемый файл Chrome."""
    for path in _chrome_candidates():
        if path.exists():
            return path
    return None


def launch_chrome(
    *,
    port: int = DEFAULT_PORT,
    profile_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Запускает Chrome с отдельным профилем и включённым CDP-портом."""
    profile = profile_dir or (ROOT / (".chrome-profile" if port == DEFAULT_PORT
                                   else f".chrome-profile-{port}"))
    profile.mkdir(parents=True, exist_ok=True)
    cdp_url = f"http://127.0.0.1:{port}"

    ok, _info = cdp_endpoint_ok(cdp_url)
    if ok and not force:
        print(f"CDP уже работает: {cdp_url}/json/version")
        return profile

    if force:
        subprocess.run(
            ["taskkill", "/F", "/IM", "chrome.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        time.sleep(1)

    exe = find_chrome()
    if exe is None:
        raise FileNotFoundError(
            "Не найден chrome.exe. Установите Google Chrome или запустите браузер вручную."
        )

    args = [
        str(exe),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    subprocess.Popen(args)
    time.sleep(2)

    ok, info = cdp_endpoint_ok(cdp_url)
    if ok:
        print(f"Chrome запущен. CDP: {cdp_url}/json/version ({info})")
    else:
        print(
            "Chrome запущен, но CDP пока не ответил.\n"
            f"Проверьте порт {port} и профиль {profile}; затем повторите запуск."
        )
    print(f"Профиль: {profile}")
    return profile


def main(argv: list[str] | None = None) -> int:
    """Обрабатывает параметры ``chrome --port ... --force`` и возвращает exit code."""
    args = list(argv or sys.argv[1:])
    force = "--force" in args or "-f" in args
    port = DEFAULT_PORT
    for index, item in enumerate(args):
        if item in {"--port", "-p"} and index + 1 < len(args):
            port = int(args[index + 1])
        elif item.startswith("--port="):
            port = int(item.split("=", 1)[1])
    try:
        launch_chrome(port=port, force=force)
    # CLI-граница обязана преобразовать любую ошибку запуска в ненулевой exit code.
    except Exception as exc:  # noqa: BLE001
        print(exc, file=sys.stderr)
        return 1
    return 0
