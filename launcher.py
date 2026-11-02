"""Оконный запуск одного или двух независимых экземпляров ParsingSite."""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

DEFAULT_WEB_PORT = 8082
DEFAULT_CDP_PORT = 9222
SETTINGS_NAME = "launcher_settings.json"
PROCESSES_NAME = "launcher_processes.json"


@dataclass(frozen=True)
class Target:
    label: str
    web_port: int
    cdp_port: int
    instance: str | None = None


@dataclass
class ManagedTarget:
    target: Target
    token: str
    process: subprocess.Popen | None = None


def port(value: str, label: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError(f"{label}: укажите порт числом.") from exc
    if not 1 <= result <= 65535:
        raise ValueError(f"{label}: порт должен быть от 1 до 65535.")
    return result


def build_targets(two: bool, values: tuple[str, str, str, str]) -> list[Target]:
    """Проверяет порты и создаёт параметры запуска без обращения к UI."""
    if not two:
        return [Target("Основная программа", DEFAULT_WEB_PORT, DEFAULT_CDP_PORT)]
    web_a, cdp_a, web_b, cdp_b = values
    targets = [
        Target("Программа 1", port(web_a, "Порт программы 1"),
               port(cdp_a, "Порт браузера 1"), "algorithm_a"),
        Target("Программа 2", port(web_b, "Порт программы 2"),
               port(cdp_b, "Порт браузера 2"), "algorithm_b"),
    ]
    all_ports = [item.web_port for item in targets] + [item.cdp_port for item in targets]
    if len(set(all_ports)) != len(all_ports):
        raise ValueError("Все четыре порта должны различаться.")
    return targets


def backend_command(path: Path) -> list[str]:
    if not path.is_file() or path.suffix.lower() not in {".exe", ".py"}:
        raise ValueError("Выберите существующий ParsingSite.exe или main.py.")
    if path.suffix.lower() == ".py" and getattr(sys, "frozen", False):
        raise ValueError("Для оконного exe выберите ParsingSite.exe.")
    return [sys.executable, str(path)] if path.suffix.lower() == ".py" else [str(path)]


def default_backend() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().with_name("ParsingSite.exe")
    return Path(__file__).resolve().with_name("main.py")


def settings_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().with_name(SETTINGS_NAME)
    return Path(__file__).resolve().with_name(SETTINGS_NAME)


def processes_path() -> Path:
    return settings_path().with_name(PROCESSES_NAME)


def launcher_request(managed: ManagedTarget, action: str, timeout: float = 2) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{managed.target.web_port}/api/launcher/{action}",
        headers={"X-Launcher-Token": managed.token},
        method="POST" if action == "stop" else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def stop_target(managed: ManagedTarget) -> None:
    """Штатно завершает только процесс, запущенный с данным ключом."""
    if not launcher_request(managed, "stop"):
        raise RuntimeError(
            f"Не удалось остановить программу на порту {managed.target.web_port}."
        )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if managed.process is not None:
            if managed.process.poll() is not None:
                return
        elif not port_occupied(managed.target.web_port):
            return
        time.sleep(0.25)
    raise RuntimeError(
        f"Программа на порту {managed.target.web_port} ещё завершает работу."
    )


def cdp_ready(cdp_port: int) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{cdp_port}/json/version", timeout=2,
        ) as response:
            return response.status == 200 and b'"Browser"' in response.read(4096)
    except (OSError, urllib.error.URLError):
        return False


def port_occupied(web_port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", web_port), timeout=0.3):
            return True
    except OSError:
        return False


def web_ready(web_port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{web_port}/api/state", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def log_path(backend: Path, target: Target) -> Path:
    folder = backend.parent / "data"
    if target.instance:
        folder = folder / "instances" / target.instance
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "launcher.log"


def start_targets(
    backend: Path, targets: list[Target], start_browser: bool,
    report: Callable[[str], None],
) -> list[ManagedTarget]:
    """Запускает Chrome, затем веб-серверы; при ошибке убирает свои серверы."""
    base_command = backend_command(backend)
    for target in targets:
        if port_occupied(target.web_port):
            raise RuntimeError(f"Порт интерфейса {target.web_port} уже занят.")

    if start_browser:
        for target in targets:
            report(f"Запуск браузера для {target.label} (CDP {target.cdp_port})…")
            completed = subprocess.run(
                [*base_command, "chrome", "--port", str(target.cdp_port)],
                cwd=backend.parent, capture_output=True, text=True,
                timeout=60, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode or not cdp_ready(target.cdp_port):
                details = (completed.stderr or completed.stdout).strip()
                raise RuntimeError(
                    f"Chrome на порту {target.cdp_port} не запустился. {details}"
                )

    started: list[ManagedTarget] = []
    try:
        for target in targets:
            report(f"Запуск: {target.label} (порт {target.web_port})…")
            arguments = [*base_command, "web", "--port", str(target.web_port)]
            if target.instance:
                arguments.extend(["--instance", target.instance,
                                  "--cdp-port", str(target.cdp_port)])
            token = secrets.token_urlsafe(32)
            environment = os.environ.copy()
            environment["PARSING_SITE_LAUNCHER_TOKEN"] = token
            with log_path(backend, target).open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    arguments, cwd=backend.parent, stdout=log, stderr=subprocess.STDOUT,
                    env=environment,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            started.append(ManagedTarget(target, token, process))

        for managed in started:
            target, process = managed.target, managed.process
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"{target.label} завершилась при запуске. Журнал: {log_path(backend, target)}"
                    )
                if web_ready(target.web_port):
                    break
                time.sleep(0.5)
            else:
                raise RuntimeError(
                    f"{target.label} не ответила за 90 секунд. Журнал: {log_path(backend, target)}"
                )
    except Exception:
        for managed in started:
            process = managed.process
            if process.poll() is None:
                process.terminate()
        raise
    return started


class LauncherApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Запуск Parsing Site")
        self.root.geometry("590x455")
        self.root.minsize(540, 370)
        self.root.configure(bg="#f5f7fa")
        self._configure_style()
        saved = self._read_settings()
        self.backend = tk.StringVar(value=str(saved.get("backend") or default_backend()))
        self.two = tk.BooleanVar(value=bool(saved.get("two", False)))
        self.browser = tk.BooleanVar(value=bool(saved.get("browser", False)))
        self.web_a = tk.StringVar(value=str(saved.get("web_a", 8082)))
        self.cdp_a = tk.StringVar(value=str(saved.get("cdp_a", 9222)))
        self.web_b = tk.StringVar(value=str(saved.get("web_b", 8083)))
        self.cdp_b = tk.StringVar(value=str(saved.get("cdp_b", 9223)))
        self.status = tk.StringVar(value="Готово к запуску")
        self._launching = False
        self._stopping = False
        self._managed = self._read_managed()
        self._build()
        self._mode_changed()
        self._refresh_stop_buttons()
        self.root.protocol("WM_DELETE_WINDOW", self._close)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Hint.TLabel", foreground="#526173")
        style.configure("Start.TButton", font=("Segoe UI", 11, "bold"), padding=(14, 8))

    def _build(self) -> None:
        body = ttk.Frame(self.root, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Parsing Site", style="Title.TLabel").pack(anchor="w")
        ttk.Label(body, text="Выберите число программ и запустите их одной кнопкой.",
                  style="Hint.TLabel").pack(anchor="w", pady=(2, 15))

        path_row = ttk.Frame(body)
        path_row.pack(fill="x", pady=(0, 14))
        ttk.Label(path_row, text="Файл программы").pack(anchor="w")
        path_entry = ttk.Frame(path_row)
        path_entry.pack(fill="x", pady=(4, 0))
        ttk.Entry(path_entry, textvariable=self.backend).pack(side="left", fill="x", expand=True)
        ttk.Button(path_entry, text="Обзор…", command=self._browse).pack(side="left", padx=(8, 0))

        mode_row = ttk.Frame(body)
        mode_row.pack(anchor="w", pady=(0, 10))
        ttk.Radiobutton(mode_row, text="Одна программа", variable=self.two, value=False,
                        command=self._mode_changed).pack(side="left", padx=(0, 22))
        ttk.Radiobutton(mode_row, text="Две программы", variable=self.two, value=True,
                        command=self._mode_changed).pack(side="left")
        self.browser_check = ttk.Checkbutton(body, text="Запустить браузер Chrome",
                                             variable=self.browser)
        self.browser_check.pack(anchor="w", pady=(0, 12))

        self.single_hint = ttk.Label(
            body, text="Стандартный запуск: интерфейс 8082, Chrome CDP 9222.",
            style="Hint.TLabel",
        )
        self.dual_frame = ttk.Frame(body)
        self._target_fields(self.dual_frame, "Программа 1 · algorithm_a", self.web_a, self.cdp_a)
        self._target_fields(self.dual_frame, "Программа 2 · algorithm_b", self.web_b, self.cdp_b)

        bottom = ttk.Frame(body)
        bottom.pack(side="bottom", fill="x", pady=(15, 0))
        self.start_button = ttk.Button(bottom, text="Запустить", style="Start.TButton",
                                       command=self._start)
        self.start_button.pack(side="right")
        ttk.Label(bottom, textvariable=self.status, wraplength=390,
                  style="Hint.TLabel").pack(side="left", fill="x", expand=True)
        self.stop_frame = ttk.Frame(body)
        self.stop_frame.pack(side="bottom", fill="x", pady=(0, 5))

    @staticmethod
    def _target_fields(parent: ttk.Frame, title: str, web: tk.StringVar,
                       cdp: tk.StringVar) -> None:
        frame = ttk.LabelFrame(parent, text=title, padding=(12, 8))
        frame.pack(fill="x", pady=(0, 8))
        ttk.Label(frame, text="Порт программы").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=web, width=10).grid(row=0, column=1, padx=(8, 25))
        ttk.Label(frame, text="CDP-порт браузера").grid(row=0, column=2, sticky="w")
        ttk.Entry(frame, textvariable=cdp, width=10).grid(row=0, column=3, padx=(8, 0))

    def _mode_changed(self) -> None:
        self.single_hint.pack_forget()
        self.dual_frame.pack_forget()
        if self.two.get():
            self.dual_frame.pack(fill="x", before=self.start_button.master)
            self.browser_check.configure(text="Запустить два браузера Chrome")
        else:
            self.single_hint.pack(anchor="w", before=self.start_button.master)
            self.browser_check.configure(text="Запустить браузер Chrome")

    def _browse(self) -> None:
        chosen = filedialog.askopenfilename(
            title="Выберите ParsingSite.exe", initialdir=str(Path(self.backend.get()).parent),
            filetypes=[("Программа", "*.exe"), ("Исходный файл", "*.py")],
        )
        if chosen:
            self.backend.set(chosen)

    def _read_settings(self) -> dict:
        try:
            data = json.loads(settings_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        payload = {
            "backend": self.backend.get(), "two": self.two.get(),
            "browser": self.browser.get(), "web_a": self.web_a.get(),
            "cdp_a": self.cdp_a.get(), "web_b": self.web_b.get(),
            "cdp_b": self.cdp_b.get(),
        }
        try:
            settings_path().write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        except OSError:
            pass  # Параметры запуска остаются доступны в текущем окне.

    def _read_managed(self) -> list[ManagedTarget]:
        try:
            entries = json.loads(processes_path().read_text(encoding="utf-8"))
            managed = [
                ManagedTarget(Target(
                    str(item["label"]), int(item["web_port"]),
                    int(item["cdp_port"]), item.get("instance"),
                ), str(item["token"]))
                for item in entries
            ]
        except (OSError, ValueError, TypeError, KeyError):
            return []
        return [item for item in managed if launcher_request(item, "status")]

    def _save_managed(self) -> None:
        entries = [
            {"label": item.target.label, "web_port": item.target.web_port,
             "cdp_port": item.target.cdp_port, "instance": item.target.instance,
             "token": item.token}
            for item in self._managed
        ]
        try:
            processes_path().write_text(json.dumps(entries, ensure_ascii=False, indent=2),
                                        encoding="utf-8")
        except OSError:
            pass

    def _refresh_stop_buttons(self) -> None:
        for child in self.stop_frame.winfo_children():
            child.destroy()
        self._managed = [
            item for item in self._managed
            if item.process is None or item.process.poll() is None
        ]
        for item in self._managed:
            ttk.Button(
                self.stop_frame,
                text=f"Остановить {item.target.label} · {item.target.web_port}",
                command=lambda selected=item: self._stop(selected),
            ).pack(side="left", padx=(0, 8))
        self._save_managed()

    def _stop(self, managed: ManagedTarget) -> None:
        if self._launching or self._stopping:
            return
        self._stopping = True
        self.start_button.configure(state="disabled")
        self.status.set(f"Остановка {managed.target.label}…")

        def run() -> None:
            try:
                stop_target(managed)
            except Exception as exc:  # noqa: BLE001 — ошибка показывается в окне.
                self.root.after(0, self._stopped, managed, str(exc))
            else:
                self.root.after(0, self._stopped, managed, None)

        threading.Thread(target=run, daemon=True).start()

    def _stopped(self, managed: ManagedTarget, error: str | None) -> None:
        self._stopping = False
        self.start_button.configure(state="normal")
        if error:
            self.status.set("Ошибка остановки")
            messagebox.showerror("Не удалось остановить", error, parent=self.root)
            return
        self._managed = [item for item in self._managed if item is not managed]
        self._refresh_stop_buttons()
        self.status.set(f"{managed.target.label} остановлена. Chrome остаётся открытым.")

    def _start(self) -> None:
        try:
            targets = build_targets(self.two.get(), (
                self.web_a.get(), self.cdp_a.get(), self.web_b.get(), self.cdp_b.get(),
            ))
            backend = Path(self.backend.get()).expanduser().resolve()
            backend_command(backend)
        except ValueError as exc:
            messagebox.showerror("Параметры запуска", str(exc), parent=self.root)
            return
        self._save_settings()
        start_browser = self.browser.get()
        if self._stopping:
            return
        self._launching = True
        self.start_button.configure(state="disabled")
        self.status.set("Подготовка запуска…")

        def report(message: str) -> None:
            self.root.after(0, self.status.set, message)

        def run() -> None:
            try:
                started = start_targets(backend, targets, start_browser, report)
            except Exception as exc:  # noqa: BLE001 — ошибка должна появиться в UI.
                self.root.after(0, self._finished, None, str(exc))
            else:
                self.root.after(0, self._finished, started, None)

        threading.Thread(target=run, daemon=True).start()

    def _finished(self, started: list[ManagedTarget] | None,
                  error: str | None) -> None:
        self._launching = False
        self.start_button.configure(state="normal")
        if error:
            self.status.set("Ошибка запуска")
            messagebox.showerror("Не удалось запустить", error, parent=self.root)
            return
        self._managed.extend(started or [])
        self._refresh_stop_buttons()
        urls = ", ".join(
            f"http://127.0.0.1:{item.target.web_port}" for item in started or []
        )
        self.status.set(f"Работает: {urls}")
        messagebox.showinfo("Программы запущены", f"Откройте интерфейс: {urls}",
                            parent=self.root)

    def _close(self) -> None:
        if self._launching or self._stopping:
            messagebox.showinfo("Операция", "Дождитесь завершения операции.", parent=self.root)
            return
        self._save_settings()
        self._save_managed()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
