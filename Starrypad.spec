# Build on the target OS: python -m PyInstaller --clean --noconfirm Starrypad.spec
from pathlib import Path
import sys
from PyInstaller.utils.hooks import copy_metadata

root = Path(SPECPATH)
if sys.platform not in ("win32", "darwin"):
    raise SystemExit("Desktop preview builds currently target Windows and macOS only")
resources = [(str(root / name), name) for name in ("assets", "samples")]
resources += [(str(root / name), ".") for name in ("LICENSE", "LICENSE-SAMPLES")]
resources += [(str(root / "docs" / "DESKTOP-PREVIEW.md"), "."),
              (str(root / "build" / "preview-metadata" / "BUILD-INFO.json"), "."),
              (str(root / "build" / "preview-metadata" / "THIRD-PARTY"), "THIRD-PARTY")]
for package in ("numpy", "pygame-ce", "sounddevice", "audiotsm", "platformdirs", "cffi", "pycparser"):
    resources += copy_metadata(package)

a = Analysis([str(root / "starrypad_desktop.py")], pathex=[str(root)],
             binaries=[], datas=resources,
             hiddenimports=["_cffi_backend", "sounddevice", "tkinter.filedialog", "tkinter.messagebox"],
             hookspath=[], runtime_hooks=[], excludes=[], noarchive=False, optimize=0)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="STARRYPAD", console=False,
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          argv_emulation=False, target_arch=None, codesign_identity=None, entitlements_file=None,
          icon=str(root / "assets" / "brand" / ("starrypad.ico" if sys.platform == "win32" else "starrypad.icns")))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="STARRYPAD")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="STARRYPAD.app", icon=str(root / "assets/brand/starrypad.icns"),
                 bundle_identifier="io.github.purinzan.starrypad", version="0.1.0",
                 info_plist={"CFBundleDisplayName": "STARRYPAD", "CFBundleVersion": "1",
                             "NSPrincipalClass": "NSApplication", "NSHighResolutionCapable": True,
                             "NSMicrophoneUsageDescription": "Record sounds you choose and play them on the pads.",
                             "LSMinimumSystemVersion": "14.0"})
