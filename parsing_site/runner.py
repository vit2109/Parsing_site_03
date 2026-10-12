"""CLI: web, chrome, check, probe, inspect, run."""

from __future__ import annotations

import logging
import sys
from time import monotonic, sleep
from typing import TYPE_CHECKING

from parsing_site.config import AppConfig, load_config

if TYPE_CHECKING:
    from parsing_site.browser import BrowserSession
    from parsing_site.tracking import TrackingSnapshot

logger = logging.getLogger("parsing_site")

USAGE = """\
Использование:
  python main.py web       — веб-интерфейс FastAPI (таблица, настройки, логи)
  python main.py test-server — тестовая SVG-карта с движущимися объектами
  python main.py chrome    — запустить Chrome с CDP (без PowerShell)
  python main.py check     — проверить CDP-подключение к Chrome на порту 9222
  python main.py probe     — таблица расстояний по HTML из data_test (без сайта)
  python main.py inspect   — координаты объектов и таблица расстояний (один снимок)
  python main.py run       — опрос в терминале по расписанию из config.toml
"""


def main(argv: list[str] | None = None) -> int:
    """Выбирает CLI-команду, обрабатывает ожидаемые ошибки и возвращает exit code."""
    _configure_stdio()
    _configure_logging()
    args = list(sys.argv[1:] if argv is None else argv)
    command = args[0] if args else "web"

    if command in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    if command not in {
        "inspect", "run", "web", "test-server", "probe", "check", "chrome"
    }:
        print(f"Неизвестная команда: {command}\n")
        print(USAGE)
        return 2

    config = load_config()
    try:
        if command == "inspect":
            return _inspect(config)
        # if command == "web":
        #     return _web()
        # if command == "test-server":
        #     return _test_server()
        if command == "web":
            return _web(args[1:])
        if command == "test-server":
            return _test_server(args[1:])
        if command == "probe":
            return _probe(config, args[1:])
        if command == "check":
            return _check_browser(config)
        if command == "chrome":
            return _launch_chrome(args[1:])
        return _run(config)
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 0
    except Exception as exc:
        from parsing_site.browser import BrowserNotConnectedError

        if isinstance(exc, BrowserNotConnectedError):
            logger.error("%s", exc)
            return 1
        logger.exception("Необработанная ошибка команды")
        return 1

def _parse_port(extra: list[str], default: int) -> int:
    """Извлекает --port N, -p N или --port=N из аргументов команды."""
    for i, item in enumerate(extra):
        if item in {"--port", "-p"}:
            if i + 1 >= len(extra):
                raise SystemExit(f"Не указано значение для {item}")
            try:
                return int(extra[i + 1])
            except ValueError:
                raise SystemExit(f"Некорректный порт: {extra[i + 1]!r}")
        if item.startswith("--port="):
            value = item.split("=", 1)[1]
            try:
                return int(value)
            except ValueError:
                raise SystemExit(f"Некорректный порт: {value!r}")
    return default


def _inspect(config: AppConfig) -> int:
    """Выполняет один живой опрос и сохраняет его терминальное/файловое представление."""
    from rich.console import Console

    from parsing_site.browser import BrowserSession
    from parsing_site.table_view import persist_snapshot, print_inspect

    with BrowserSession(config.browser) as session:
        _log_tabs(session)
        snapshot = _take_snapshot(session, config)

    console = Console(soft_wrap=True)
    print_inspect(snapshot, console)
    html_path = persist_snapshot(
        snapshot, config.output.data_dir, config.schedule.interval_seconds
    )
    console.print(f"\nHTML-таблица: {html_path}")
    return 0


def _run(config: AppConfig) -> int:
    """Непрерывно обновляет таблицу в терминале с заданным интервалом."""
    from rich.console import Console
    from rich.live import Live

    from parsing_site.browser import BrowserSession
    from parsing_site.table_view import build_rich_view, persist_snapshot

    interval = max(0.4, config.schedule.interval_seconds)
    console = Console(soft_wrap=True)
    console.print(
        f"Опрос каждые {interval:g} с. HTML: {config.output.data_dir / 'distances.html'}\n"
        "Остановка: Ctrl+C\n"
    )

    with BrowserSession(config.browser) as session:
        _log_tabs(session)
        first = _take_snapshot(session, config)
        persist_snapshot(first, config.output.data_dir, interval)
        with Live(
            build_rich_view(first),
            console=console,
            refresh_per_second=4,
            screen=False,
        ) as live:
            while True:
                started = monotonic()
                snapshot = _take_snapshot(session, config)
                persist_snapshot(snapshot, config.output.data_dir, interval)
                live.update(build_rich_view(snapshot))
                remaining = interval - (monotonic() - started)
                if remaining > 0:
                    sleep(remaining)
    return 0


def _take_snapshot(session: BrowserSession, config: AppConfig) -> TrackingSnapshot:
    """Создаёт снимок активной вкладки с настройками текущего запуска."""
    from parsing_site.tracking import capture_snapshot

    return capture_snapshot(session, config.tracking)


def _web(extra: list[str]) -> int:
    """Запускает локальный FastAPI-интерфейс через Uvicorn."""
    import uvicorn

    port = _parse_port(extra, 8082)

    print("Интерфейс: http://127.0.0.1:8082")
    uvicorn.run("parsing_site.webapp:app", host="127.0.0.1", port=port, reload=False)
    return 0


def _test_server(extra: list[str]) -> int:
    """Запускает локальную SVG-карту для сквозной проверки мониторинга."""
    import uvicorn

    port = _parse_port(extra, 8000)
    print("Тестовая карта: http://127.0.0.1:8000")
    uvicorn.run(
        "parsing_site.test_server:app", host="127.0.0.1", port=port, reload=False
    )
    return 0


def _probe(config: AppConfig, extra: list[str]) -> int:
    """Разбирает сохранённый HTML без подключения к браузеру."""
    from pathlib import Path

    from parsing_site.config import ROOT
    from parsing_site.html_probe import analyze_html_file, snapshot_from_html_file
    from parsing_site.table_view import print_inspect

    json_mode = "--json" in extra
    paths = [item for item in extra if not item.startswith("-")]
    path = Path(paths[0]) if paths else ROOT / "data_test" / "1.html"
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        print(f"Файл не найден: {path}")
        return 1
    if json_mode:
        import json

        print(json.dumps(analyze_html_file(path), ensure_ascii=False, indent=2))
        return 0
    snapshot = snapshot_from_html_file(path, config.tracking)
    print_inspect(snapshot)
    print(
        "\nЭто снимок HTML без живой карты. Для опроса сайта откройте вкладку "
        "в Chrome с CDP и нажмите «Запустить»."
    )
    return 0


def _launch_chrome(extra: list[str]) -> int:
    """Передаёт параметры встроенному запускателю Chrome."""
    from parsing_site.launch_chrome import main as launch_main

    return launch_main(extra)


def _check_browser(config: AppConfig) -> int:
    """Проверяет CDP и выводит список доступных вкладок."""
    from parsing_site.browser import BrowserSession
    from parsing_site.cdp_check import cdp_endpoint_ok

    cdp = config.browser.cdp_url
    ok, info = cdp_endpoint_ok(cdp)
    if not ok:
        from parsing_site.cdp_check import format_cdp_help

        print(format_cdp_help(cdp, info))
        return 1
    print(f"CDP OK: {info}")
    try:
        with BrowserSession(config.browser) as session:
            tabs = session.list_tabs()
            print(f"Вкладок: {len(tabs)}")
            for url, title in tabs:
                print(f"  - {title or '(без заголовка)'} — {url}")
    # Команда проверки сообщает пользователю и сетевые, и Playwright-ошибки одинаково.
    except Exception as exc:  # noqa: BLE001
        print(f"Playwright: {exc}")
        return 1
    return 0


def _log_tabs(session: BrowserSession) -> None:
    """Записывает доступные вкладки в журнал для диагностики выбора URL."""
    tabs = session.list_tabs()
    if not tabs:
        return
    logger.info("Открытые вкладки (%s):", len(tabs))
    for url, title in tabs:
        label = title or "(без заголовка)"
        logger.info("  - %s -- %s", label, url)


def _configure_stdio() -> None:
    """Настраивает UTF-8 для русскоязычного вывода в поддерживаемых терминалах."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def _configure_logging() -> None:
    """Включает единый формат сообщений командной строки."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
