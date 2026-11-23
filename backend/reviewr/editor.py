"""Open a file at a line in the user's editor."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from reviewr.config import Config


class EditorError(RuntimeError):
    pass


# Where editors' command-line launchers live inside their app bundles, for when the
# command isn't on PATH (e.g. VS Code's "Install 'code' command in PATH" was never run).
_BUNDLED = {
    "code": ["Visual Studio Code.app/Contents/Resources/app/bin/code",
             "Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code"],
    "code-insiders": ["Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code-insiders"],
    "cursor": ["Cursor.app/Contents/Resources/app/bin/cursor"],
    "windsurf": ["Windsurf.app/Contents/Resources/app/bin/windsurf"],
    "zed": ["Zed.app/Contents/MacOS/cli"],
    "pycharm": ["PyCharm.app/Contents/MacOS/pycharm", "PyCharm CE.app/Contents/MacOS/pycharm",
                "PyCharm Professional Edition.app/Contents/MacOS/pycharm"],
    "idea": ["IntelliJ IDEA.app/Contents/MacOS/idea", "IntelliJ IDEA CE.app/Contents/MacOS/idea"],
    "webstorm": ["WebStorm.app/Contents/MacOS/webstorm"],
    "subl": ["Sublime Text.app/Contents/SharedSupport/bin/subl"],
}


def find_command(cmd: str, app_dirs: list[Path] | None = None) -> str | None:
    """The editor command: on PATH, or inside its app bundle in /Applications or
    ~/Applications."""
    if found := shutil.which(cmd):
        return found
    dirs = app_dirs if app_dirs is not None else [Path("/Applications"), Path.home() / "Applications"]
    for rel in _BUNDLED.get(Path(cmd).name, []):
        for d in dirs:
            if (d / rel).is_file():
                return str(d / rel)
    return None


def editor_argv(template: list[str], root: Path, file: Path | None, line: int | None) -> list[str]:
    """Fill an editor template. {root} is the worktree folder, opened as the project;
    {path} and {line} are the file inside it. With no file, only the folder is opened:
    template parts that mention {path} or {line} are dropped, along with a flag that
    directly precedes one (such as --goto or --line)."""
    if file is not None:
        return [p.format(root=str(root), path=str(file), line=line or 1) for p in template]
    out: list[str] = []
    for p in template:
        if "{path}" in p or "{line}" in p:
            if out and out[-1].startswith("-") and "{" not in out[-1]:
                out.pop()
            continue
        out.append(p.format(root=str(root)))
    if str(root) not in out:
        out.append(str(root))
    return out


def open_in_editor(config: Config, editor: str | None, root: Path, file: Path | None, line: int | None) -> list[str]:
    name = editor or config.default_editor
    template = config.editors.get(name)
    if template is None:
        raise EditorError(f"Unknown editor {name!r}. Configured: {', '.join(config.editors)}")
    if file is not None and not file.exists():
        raise EditorError(f"{file} does not exist in {root}")
    argv = editor_argv(template, root, file, line)
    found = find_command(argv[0])
    if found is None:
        raise EditorError(
            f"{argv[0]!r} isn't on PATH and wasn't found in /Applications. Install its command-line launcher, "
            f"or set the full path to it under Settings → Editors."
        )
    argv[0] = found
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return argv
