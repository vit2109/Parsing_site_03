"""Кроссплатформенный звуковой сигнал для ручной проверки интерфейса."""

from __future__ import annotations

import platform
import subprocess


def play_system_beep() -> bool:
    """Воспроизводит короткий сигнал и сообщает, удалось ли запустить механизм ОС."""
    system = platform.system()
    try:
        if system == "Windows":
            import winsound

            winsound.Beep(1000, 200)
            winsound.Beep(1200, 150)
            return True
        if system == "Darwin":
            result = subprocess.run(
                ["say", "beep"], capture_output=True, timeout=1, check=False
            )
            return result.returncode == 0
        if system == "Linux":
            result = subprocess.run(
                ["beep", "-f", "1000", "-l", "200"],
                capture_output=True,
                timeout=1,
                check=False,
            )
            if result.returncode == 0:
                return True
    except (OSError, subprocess.SubprocessError):
        # Терминальный bell остаётся безопасным резервным вариантом, когда утилиты нет.
        pass

    print("\a", end="", flush=True)
    return system in {"Windows", "Darwin", "Linux"}
