import os
import tempfile
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

project_root = Path(SPEC).resolve().parent.parent
matplotlib_cache = Path(tempfile.gettempdir()) / "control-combination-matplotlib-build"
matplotlib_cache.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(matplotlib_cache))

datas = [
    (str(project_root / "frontend" / "dist"), "frontend/dist"),
    *collect_data_files("pyfixest"),
    *collect_data_files("maketables"),
    *collect_data_files("great_tables"),
    *collect_data_files("faicons"),
    *copy_metadata("pyfixest", recursive=True),
]
binaries = []
hiddenimports = collect_submodules(
    "pyfixest",
    filter=lambda name: not any(
        excluded in name
        for excluded in (".tests", ".torch", ".cupy", ".quantreg")
    ),
)
hiddenimports.append("matplotlib.backends.backend_pdf")

a = Analysis(
    [str(project_root / "run_app.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["Agg"]}},
    runtime_hooks=[],
    excludes=["pytest", "notebook", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ControlCombinationExplorer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ControlCombinationExplorer",
)

