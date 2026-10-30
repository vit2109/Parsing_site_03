# -*- mode: python ; coding: utf-8 -*-
# Сборка одного exe:
#   pip install pyinstaller
#   pyinstaller parsing_site.spec
# Результат: dist/ParsingSite.exe

from pathlib import Path

block_cipher = None
project_dir = Path(SPECPATH)

datas = [
    (str(project_dir / "web"), "web"),
    (str(project_dir / "config.toml"), "."),
    (str(project_dir / "start_chrome.ps1"), "."),
    (str(project_dir / "start_chrome.bat"), "."),
    (str(project_dir / "certs" / "ca-bundle.pem"), "certs"),
]

hiddenimports = [
    "parsing_site",
    "parsing_site.browser",
    "parsing_site.config",
    "parsing_site.coordinates",
    "parsing_site.db",
    "parsing_site.html_probe",
    "parsing_site.history_export",
    "parsing_site.history_capture",
    "parsing_site.history_report",
    "parsing_site.parser",
    "parsing_site.processor",
    "parsing_site.runner",
    "parsing_site.settings_store",
    "parsing_site.simulator",
    "parsing_site.sound",
    "parsing_site.table_history_db",
    "parsing_site.table_view",
    "parsing_site.test_server",
    "parsing_site.tracking",
    "parsing_site.webapp",
    "parsing_site.launch_chrome",
    "parsing_site.worker",
    "uvicorn",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.importer",
    "fastapi",
    "starlette",
    "starlette.routing",
    "starlette.staticfiles",
    "starlette.templating",
    "jinja2",
    "playwright",
    "playwright.sync_api",
    "playwright._impl",
    "greenlet",
    "multipart",
    "rich",
    "bs4",
    "lxml",
    "lxml.etree",
    "pandas",
]

a = Analysis(
    [str(project_dir / "main.py")],
    pathex=[str(project_dir)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ParsingSite",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
