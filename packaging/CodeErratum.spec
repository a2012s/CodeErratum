# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — one double-clickable app, no external Python needed.

Two data directories are load-bearing and easy to forget:

* ``codeerratum/web/static``  — the UI; without it the window opens on a 404.
* ``codeerratum/prompts``     — the review prompts, read as files at runtime;
  without them the app starts and paints fine, then fails only once someone
  spends tokens on a review. ``--selftest`` (run by packaging/build.py) is the
  guard for exactly this.

Both are placed relative to the bundle root (``sys._MEIPASS``), which is where
``Path(__file__).parent`` lands for a frozen module — so the source-path logic
in ``server._locate_static`` and the ``PROMPT_PATH`` constants keep working
unchanged instead of needing a frozen special case.

Onedir on purpose: ``--onefile`` unpacks to a temp directory on every launch
(slow cold start, and antivirus treats it as a dropper).
"""

import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - injected by PyInstaller
INIT = (ROOT / "src" / "codeerratum" / "__init__.py").read_text(encoding="utf-8")
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', INIT).group(1)
# Same source of truth as /api/meta and the in-app About dialog.
HOMEPAGE = re.search(r'HOMEPAGE\s*=\s*"([^"]+)"', INIT).group(1)

# pywebview imports its backend through importlib, which static analysis cannot
# follow: without these the frozen app raises ImportError while opening a window.
BACKENDS = {
    "darwin": ["webview.platforms.cocoa"],
    "win32": ["webview.platforms.edgechromium", "webview.platforms.winforms"],
}.get(sys.platform, ["webview.platforms.gtk", "webview.platforms.qt"])

DATAS = [
    (str(ROOT / "src" / "codeerratum" / "web" / "static"), "codeerratum/web/static"),
    (str(ROOT / "src" / "codeerratum" / "prompts"), "codeerratum/prompts"),
]
if sys.platform == "win32":
    # The EdgeChromium backend is a .NET assembly bridge: WebView2Loader.dll and
    # Microsoft.Web.WebView2.Core.dll ship *inside* the pywebview package and are
    # loaded by path at runtime. PyInstaller copies .py files only, so a Windows
    # build without these starts, then dies the moment it opens a window.
    from PyInstaller.utils.hooks import collect_data_files  # noqa: PLC0415

    DATAS += collect_data_files("webview")

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "desktop_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=DATAS,
    hiddenimports=["webview", *BACKENDS],
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "pytest_asyncio", "ruff", "PyInstaller"],
    noarchive=False,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CodeErratum",
    debug=False,
    strip=False,
    upx=False,
    console=False,  # GUI app: no terminal window on any platform
    argv_emulation=False,
    # Windows draws the taskbar/Explorer icon from the .exe itself; macOS ignores
    # this and takes the .icns through BUNDLE below. Built from the same 1024px
    # artwork, six sizes, PNG-compressed.
    icon=str(ROOT / "packaging" / "icon" / "CodeErratum.ico")
    if sys.platform == "win32" and (ROOT / "packaging" / "icon" / "CodeErratum.ico").is_file()
    else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="CodeErratum",
)

if sys.platform == "darwin":
    # Built from packaging/icon/icon-1024.png: the tile is clipped to a rounded
    # square and scaled into the 824px safe area, so the Dock shows the same
    # silhouette as a native app. Falls back to the generic icon if the artwork
    # is missing, which keeps a fresh clone buildable.
    icns = ROOT / "packaging" / "icon" / "CodeErratum.icns"
    app = BUNDLE(  # noqa: F821
        coll,
        name="CodeErratum.app",
        icon=str(icns) if icns.is_file() else None,
        bundle_identifier="com.codeerratum.app",
        info_plist={
            "CFBundleName": "CodeErratum",
            "CFBundleDisplayName": "CodeErratum",
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            # Bottom line of the system About panel (App menu → About
            # CodeErratum). The panel cannot render a clickable link, so the
            # address is here to be read; the in-app About dialog opens it.
            "NSHumanReadableCopyright": f"MIT License · {HOMEPAGE}",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "LSApplicationCategoryType": "public.app-category.developer-tools",
            # The window is a WebView; without this the text renders at 1x on
            # Retina displays and looks broken next to native chrome.
            "NSRequiresAquaSystemAppearance": False,
        },
    )
