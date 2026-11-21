# PyInstaller spec for the backend in the packaged app. Run by scripts/package.sh:
#
#   cd backend && uv run --group build pyinstaller --noconfirm packaging/reviewr.spec
#
# Produces dist/reviewr/ (one folder, not one file, so startup doesn't unpack anything).
# electron-builder copies it to reviewr.app/Contents/Resources/backend/reviewr/.
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = (
    # uvicorn imports the app from the string "reviewr.api:app", and its protocol and
    # loop implementations by name, so the analysis can't see them.
    collect_submodules("reviewr")
    + collect_submodules("uvicorn")
)
datas = (
    collect_data_files("reviewr")  # github/queries/*.graphql
    # Includes _bundled/claude, the Claude Code CLI the Agent SDK runs.
    + collect_data_files("claude_agent_sdk")
)

a = Analysis(
    ["entry.py"],
    hiddenimports=hiddenimports,
    datas=datas,
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="reviewr", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="reviewr")
