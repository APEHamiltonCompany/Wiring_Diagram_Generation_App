# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Wire Diagram Builder.

Build (on Windows):
    pip install pyinstaller openpyxl
    pyinstaller wirediagram.spec

Produces a single windowed executable: dist/WireDiagramBuilder.exe
The newest DRD and StyleGuide HTML in data/, drd.json and samples/ are
bundled. build_exe.bat rebuilds drd.json from the newest DRD HTML first.
"""
import os

block_cipher = None

import glob


def _newest(pattern):
    """Newest DeviceReferenceDatabase_v*.html / StyleGuide_v*.html in data/."""
    import re

    def ver(p):
        m = re.search(r"_v([0-9]+(?:\.[0-9]+)+)\.html$", p)
        return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)
    files = sorted(glob.glob(pattern), key=ver)
    return files[-1:] if files else []


# Bundle ONLY shipped, read-only data. Per-user files (pn_overrides.json,
# user_devices.json, a rebuilt drd.json) live in %APPDATA%\WireDiagramBuilder
# at runtime and are never bundled.
datas = [(p, "data") for p in
         ["data/drd.json"]
         + _newest("data/DeviceReferenceDatabase_v*.html")
         + _newest("data/StyleGuide_v*.html")
         + ["data/Power_Distribution_Analysis_Summary.txt"]]
datas += [("samples", "samples")]
datas = [(src, dst) for (src, dst) in datas if os.path.exists(src)]
print("Bundling:", [s for s, _ in datas])

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=["openpyxl"],
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
    name="WireDiagramBuilder",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,          # windowed GUI app (no console window)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
