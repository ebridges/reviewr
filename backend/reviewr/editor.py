"""Open a file at a line in the user's editor."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from reviewr.config import Config


class EditorError(RuntimeError):
    pass


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
    if shutil.which(argv[0]) is None:
        raise EditorError(f"{argv[0]!r} is not on PATH. Install its command-line launcher.")
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return argv
