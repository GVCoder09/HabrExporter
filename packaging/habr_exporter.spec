# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: a small one-folder build of the GUI (``HabrExporter``) and the CLI
(``HabrExporter-cli``) sharing one set of libraries. Works on Windows and Linux.

    pyinstaller --noconfirm --clean packaging/habr_exporter.spec

What is trimmed (the bulk of the size is the Chromium engine of Qt WebEngine, which cannot go):

* Python and Qt modules the app never imports (see ``EXCLUDES``);
* Qt plugins, QML, translations and Chromium locales that are not used (``DROP``, ``KEEP``);
* docstrings and asserts (``optimize=2``), debug symbols on Linux (``strip``).

UPX is deliberately off: it corrupts Qt/Chromium libraries and trips antivirus software.
Pack the result with maximum compression (see ``.github/workflows/build.yml``).
"""

import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821  (SPECPATH is injected by PyInstaller)
SRC = ROOT / "src"
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

# --- Modules that are never imported -----------------------------------------------------

QT_UNUSED = [
    "Qt3DAnimation", "Qt3DCore", "Qt3DExtras", "Qt3DInput", "Qt3DLogic", "Qt3DRender",
    "QtBluetooth", "QtCharts", "QtDataVisualization", "QtDesigner", "QtGraphs", "QtHelp",
    "QtHttpServer", "QtMultimedia", "QtMultimediaWidgets", "QtNetworkAuth", "QtNfc",
    "QtPdf", "QtPdfWidgets", "QtQml", "QtQuick", "QtQuick3D", "QtQuickWidgets",
    "QtRemoteObjects", "QtSensors", "QtSerialPort", "QtSpatialAudio", "QtSql",
    "QtStateMachine", "QtSvgWidgets", "QtTest", "QtTextToSpeech", "QtWebSockets", "QtXml",
]
EXCLUDES = (
    [f"PyQt6.{name}" for name in QT_UNUSED]
    + [
        # the GUI toolkit of the standard library, test and packaging machinery
        "tkinter", "_tkinter", "turtle", "idlelib", "lib2to3", "pydoc", "pydoc_data", "doctest",
        "unittest", "test", "distutils", "setuptools", "pkg_resources", "pip", "wheel",
        "ensurepip", "venv", "sqlite3", "curses", "readline",
        # optional libraries of the build environment that the app never uses (Pillow and others
        # import numpy "if available", so it gets bundled from a global Python otherwise)
        "numpy", "scipy", "pandas", "matplotlib", "PyQt5", "PySide2", "PySide6",
        # Pillow helpers for other toolkits; lxml parts the parser does not use
        "PIL.ImageTk", "PIL._tkinter_finder", "PIL.ImageQt", "PIL.ImageGrab", "PIL._avif",
        "lxml.objectify", "lxml.html.soupparser", "lxml.html.html5parser", "lxml.html.diff",
        "lxml.isoschematron",
    ]
)

# --- Files to drop from the collected binaries and data ----------------------------------

DROP = [re.compile(p, re.I) for p in (
    r"(^|/)Qt6?/qml/",                                  # QML modules (no QML in the app)
    r"(^|/)translations/.*\.qm$",                       # Qt translations (a few are kept below)
    r"(^|/)qtwebengine_locales/.*\.pak$",               # Chromium locales (two are kept below)
    r"qtwebengine_devtools_resources\.pak$",            # Chromium DevTools
    r"\.debug\.(pak|bin)$",                             # debug copies of the Chromium resources
    r"(^|/)plugins/(?!platforms|styles|imageformats|iconengines|tls|xcbglintegrations"
    r"|platformthemes|platforminputcontexts|wayland)",  # every Qt plugin the GUI does not load
    r"(^|/)opengl32sw\.dll$",                           # 20 MB software OpenGL (Widgets use raster)
    r"(^|/)(lib)?Qt6(Quick3D|3D|Multimedia|Pdf|Sql|Test|Bluetooth|Nfc|Sensors|SerialPort|"
    r"Charts|DataVisualization|Graphs|Designer|Help|RemoteObjects|HttpServer|"
    r"TextToSpeech|WebSockets|NetworkAuth|SpatialAudio|StateMachine)\w*\.(dll|so[.\d]*)$",
    r"\.pyi$",                                          # type stubs
    r"(^|/)PyQt6/bindings/",                            # sip sources
    r"(^|/)PyQt6/Qt6/(include|lib/cmake|libexec/(?!QtWebEngineProcess))",
    r"(^|/)(__pycache__|tests?)/",
)]
KEEP = [re.compile(p, re.I) for p in (
    r"(^|/)translations/(qt|qtbase|qtwebengine)_(ru|en)\.qm$",
    r"(^|/)qtwebengine_locales/(en-US|ru)\.pak$",
)]


def wanted(entry):
    """Return False for a TOC entry (dest, src, typecode) that matches ``DROP`` and not ``KEEP``."""
    dest = entry[0].replace("\\", "/")
    if any(p.search(dest) for p in KEEP):
        return True
    return not any(p.search(dest) for p in DROP)


def make_analysis(script):
    return Analysis(  # noqa: F821
        [str(ROOT / "packaging" / script)],
        pathex=[str(SRC)],
        datas=[(str(SRC / "habr_exporter" / "assets"), "habr_exporter/assets")],
        hiddenimports=["PyQt6.QtSvg", "PyQt6.QtWebEngineWidgets", "PyQt6.QtWebEngineCore"],
        excludes=EXCLUDES,
        noarchive=False,
        optimize=2,
    )


gui = make_analysis("launcher.py")
cli = make_analysis("launcher_cli.py")
MERGE((gui, "HabrExporter", "HabrExporter"), (cli, "HabrExporter-cli", "HabrExporter-cli"))  # noqa: F821

for analysis in (gui, cli):
    analysis.binaries = [e for e in analysis.binaries if wanted(e)]
    analysis.datas = [e for e in analysis.datas if wanted(e)]

icon = [str(SRC / "habr_exporter" / "assets" / "logo.ico")] if IS_WINDOWS else None
strip = IS_LINUX

gui_pyz = PYZ(gui.pure)  # noqa: F821
cli_pyz = PYZ(cli.pure)  # noqa: F821

gui_exe = EXE(  # noqa: F821
    gui_pyz, gui.scripts, [], exclude_binaries=True, name="HabrExporter",
    console=False, icon=icon, strip=strip, upx=False,
)
cli_exe = EXE(  # noqa: F821
    cli_pyz, cli.scripts, [], exclude_binaries=True, name="HabrExporter-cli",
    console=True, icon=icon, strip=strip, upx=False,
)

COLLECT(  # noqa: F821
    gui_exe, gui.binaries, gui.datas,
    cli_exe, cli.binaries, cli.datas,
    strip=strip, upx=False, name="HabrExporter",
)
