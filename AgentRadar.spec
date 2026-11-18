# Build from the repository root: python -m PyInstaller AgentRadar.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files, collect_submodules
from pathlib import Path
import sys

datas = [('config.json', '.'), ('agentradar/assets', 'agentradar/assets')]
datas += collect_data_files('webview')
a = Analysis(['desktop.py'], pathex=[], binaries=[], datas=datas,
             hiddenimports=collect_submodules('uvicorn') + ['keyring.backends.Windows'],
             hookspath=[], hooksconfig={}, runtime_hooks=[],
             excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'pytest', 'tkinter'], noarchive=False)
# A Conda interpreter links pyexpat against its own libexpat. A different
# copy found on PATH can have the same filename and incompatible exports.
expat = Path(sys.base_prefix) / 'Library' / 'bin' / 'libexpat.dll'
if expat.is_file():
    a.binaries = [(name, str(expat) if name.lower() == 'libexpat.dll' else source, kind)
                  for name, source, kind in a.binaries]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='AgentRadar', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='AgentRadar')
