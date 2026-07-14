# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from importlib.metadata import version


spec_dir = Path(SPECPATH)
project_root = spec_dir.parent.parent
entrypoint = spec_dir / "entrypoint.py"
app_version = version("spar-cli")

a = Analysis(
    [str(entrypoint)],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Spar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Spar",
)
app = BUNDLE(
    coll,
    name="Spar.app",
    bundle_identifier="pl.marekbartczak.spar",
    version=app_version,
    target_arch="x86_64",
    codesign_identity=None,
    entitlements_file=None,
    info_plist={
        "CFBundleDisplayName": "Spar",
        "CFBundleName": "Spar",
        "CFBundleShortVersionString": app_version,
        "NSHighResolutionCapable": True,
    },
)
