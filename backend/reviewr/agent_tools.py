"""The coding agent's tools, confined to one worktree.

The agent gets Anthropic's text editor tool (view, create, str_replace, insert) and two
of reviewr's own: find_files (by glob) and search (regex over file contents). Every path
comes from the model, so each one is resolved and must stay inside the worktree;
symlinks that lead out of it (the dependency folders reviewr links in) can't be
followed, and .git is off limits. There's no shell.
"""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path
from typing import Any

EDITOR = {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool", "max_characters": 60_000}
FIND = {
    "name": "find_files",
    "description": (
        "List files in the repository whose path matches a glob, e.g. 'src/**/*.ts' or '*test*'. "
        "Only files tracked by git or not ignored are listed. Returns repo-relative paths."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"pattern": {"type": "string", "description": "Glob matched against repo-relative paths"}},
        "required": ["pattern"],
        "additionalProperties": False,
    },
}
SEARCH = {
    "name": "search",
    "description": (
        "Search file contents with a regular expression (POSIX extended, like grep -E). Returns "
        "matching lines as path:line:text. Use `glob` to limit the files searched ('' for all)."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regular expression"},
            "glob": {"type": "string", "description": "Glob for the files to search, or '' for all files"},
        },
        "required": ["pattern", "glob"],
        "additionalProperties": False,
    },
}
TOOLS = [EDITOR, FIND, SEARCH]

MAX_LIST = 400
MAX_MATCHES = 300


class ToolError(Exception):
    """Returned to the model as an error tool result so it can correct itself."""


class Workspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.changed: set[str] = set()

    # Paths

    def resolve(self, path: str) -> Path:
        if not path:
            raise ToolError("A path is required.")
        raw = Path(path)
        p = (raw if raw.is_absolute() else self.root / raw).resolve()
        if p != self.root and not p.is_relative_to(self.root):
            raise ToolError(f"{path} is outside the repository; only files inside it can be used.")
        rel = p.relative_to(self.root)
        if rel.parts and rel.parts[0] == ".git":
            raise ToolError("The .git folder can't be read or changed.")
        return p

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root)) or "."

    def _files(self) -> list[str]:
        out = subprocess.run(
            ["git", "-C", str(self.root), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True, text=True, check=False,
        ).stdout
        return [f for f in out.splitlines() if f]

    # Dispatch

    def run(self, name: str, args: dict[str, Any]) -> str:
        if name == EDITOR["name"]:
            return self.editor(args)
        if name == FIND["name"]:
            return self.find(args["pattern"])
        if name == SEARCH["name"]:
            return self.search(args["pattern"], args.get("glob", ""))
        raise ToolError(f"Unknown tool {name}.")

    # Text editor

    def editor(self, a: dict[str, Any]) -> str:
        cmd = a.get("command")
        path = self.resolve(a.get("path", ""))
        if cmd == "view":
            return self._view(path, a.get("view_range"))
        if cmd == "create":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(a.get("file_text", ""))
            self.changed.add(self.rel(path))
            return f"Created {self.rel(path)}."
        if not path.is_file():
            raise ToolError(f"{self.rel(path)} doesn't exist.")
        text = path.read_text(errors="replace")
        if cmd == "str_replace":
            old, new = a.get("old_str", ""), a.get("new_str", "")
            count = text.count(old) if old else 0
            if count == 0:
                raise ToolError(f"old_str wasn't found in {self.rel(path)}. View the file and copy the text exactly.")
            if count > 1:
                raise ToolError(f"old_str appears {count} times in {self.rel(path)}; include more surrounding lines so it's unique.")
            path.write_text(text.replace(old, new, 1))
            self.changed.add(self.rel(path))
            return f"Edited {self.rel(path)}."
        if cmd == "insert":
            lines = text.splitlines(keepends=True)
            at = int(a.get("insert_line", 0))
            if not 0 <= at <= len(lines):
                raise ToolError(f"insert_line must be between 0 and {len(lines)}.")
            insert = a.get("insert_text", "")
            if insert and not insert.endswith("\n"):
                insert += "\n"
            if at and lines and not lines[at - 1].endswith("\n"):
                lines[at - 1] += "\n"
            lines.insert(at, insert)
            path.write_text("".join(lines))
            self.changed.add(self.rel(path))
            return f"Inserted after line {at} of {self.rel(path)}."
        raise ToolError(f"Unknown editor command {cmd!r}.")

    def _view(self, path: Path, view_range: list[int] | None) -> str:
        if path.is_dir():
            prefix = "" if path == self.root else self.rel(path) + "/"
            entries = sorted({f[len(prefix):].split("/")[0] + ("/" if "/" in f[len(prefix):] else "")
                              for f in self._files() if f.startswith(prefix)})
            more = f"\n… and {len(entries) - MAX_LIST} more" if len(entries) > MAX_LIST else ""
            return "\n".join(entries[:MAX_LIST]) + more
        if not path.is_file():
            raise ToolError(f"{self.rel(path)} doesn't exist.")
        lines = path.read_text(errors="replace").splitlines()
        start, end = 1, len(lines)
        if view_range:
            start = max(1, int(view_range[0]))
            end = len(lines) if int(view_range[1]) == -1 else min(len(lines), int(view_range[1]))
        return "\n".join(f"{n}\t{lines[n - 1]}" for n in range(start, end + 1)) or "(empty file)"

    # reviewr's tools

    def find(self, pattern: str) -> str:
        hits = [f for f in self._files() if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(f.rsplit("/", 1)[-1], pattern)]
        if not hits:
            return "No files match."
        more = f"\n… and {len(hits) - MAX_LIST} more; narrow the pattern" if len(hits) > MAX_LIST else ""
        return "\n".join(hits[:MAX_LIST]) + more

    def search(self, pattern: str, glob: str) -> str:
        argv = ["git", "-C", str(self.root), "grep", "-n", "-I", "-E", "--untracked", "-e", pattern]
        if glob:
            argv += ["--", f":(glob){glob}"]
        proc = subprocess.run(argv, capture_output=True, text=True, errors="replace", check=False)
        if proc.returncode == 1:
            return "No matches."
        if proc.returncode != 0:
            raise ToolError(f"The search failed: {proc.stderr.strip()[:300]}")
        lines = proc.stdout.splitlines()
        more = f"\n… and {len(lines) - MAX_MATCHES} more; narrow the pattern or glob" if len(lines) > MAX_MATCHES else ""
        return "\n".join(line[:400] for line in lines[:MAX_MATCHES]) + more
