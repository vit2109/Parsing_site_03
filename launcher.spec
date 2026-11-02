# -*- mode: python ; coding: utf-8 -*-
"""Отдельный оконный exe для запуска ParsingSite.exe из той же папки."""

from pathlib import Path

project_dir = Path(SPECPATH)

a = Analysis(
    [str(project_dir / "launcher.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name="ParsingSiteLauncher", debug=False, bootloader_ignore_signals=False,
    strip=False, upx=True, console=False,
)
