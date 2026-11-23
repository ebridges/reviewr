"""Your login shell's environment, for when reviewr is started from Finder or the Dock.

macOS starts GUI apps with launchd's environment, not your shell's: PATH misses what
your shell profile adds (e.g. VS Code's `code`), and SSH_AUTH_SOCK is the system agent,
not the one your profile points at. Git then can't sign commits with your SSH key
("Couldn't find key in agent"), and editors aren't found.

The desktop app sets REVIEWR_GUI=1 for the backend; `reviewr serve` then reads the
environment of a login shell once at startup and adopts it, as VS Code does. Started
from a terminal, the backend already has that environment and nothing changes.
"""

from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger(__name__)

_MARK = "__REVIEWR_ENV_7f3a__"
TIMEOUT_S = 10
# Per-process or per-terminal values that shouldn't come from a throwaway shell.
_SKIP = {"PWD", "OLDPWD", "SHLVL", "_", "TERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TERM_SESSION_ID",
         "COLUMNS", "LINES", "PS1", "PS2", "PROMPT", "RPROMPT", "ZDOTDIR_OLD", "TMPDIR"}
# Set by the desktop app or this process; ours win.
_OURS_PREFIX = ("REVIEWR_", "ELECTRON_")
_OURS = {"PYTHONUNBUFFERED"}


def read_login_env(shell: str | None = None, timeout: float = TIMEOUT_S) -> dict[str, str] | None:
    """The environment of an interactive login shell (what a new terminal has), or None
    if it can't be read. Profile output around it is ignored."""
    shell = shell or os.environ.get("SHELL") or "/bin/zsh"
    script = f"printf '%s' '{_MARK}'; env -0; printf '%s' '{_MARK}'"
    try:
        proc = subprocess.run([shell, "-ilc", script], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("Couldn't read your login shell's environment (%s): %s", shell, e)
        return None
    out = proc.stdout.decode(errors="replace")
    parts = out.split(_MARK)
    if len(parts) < 3:
        log.warning("Couldn't read your login shell's environment (%s exited %s)", shell, proc.returncode)
        return None
    env = {}
    for entry in parts[-2].split("\0"):
        key, sep, value = entry.partition("=")
        if sep and key:
            env[key] = value
    return env


def merge(current: dict[str, str], shell: dict[str, str]) -> dict[str, str]:
    """`current` with the shell's values, except per-terminal ones and reviewr's own.
    PATH is the shell's, followed by any of the current entries it lacks."""
    out = dict(current)
    for key, value in shell.items():
        if key in _SKIP or key in _OURS or key.startswith(_OURS_PREFIX):
            continue
        if key == "PATH":
            mine = [p for p in current.get("PATH", "").split(os.pathsep) if p]
            theirs = [p for p in value.split(os.pathsep) if p]
            out["PATH"] = os.pathsep.join(theirs + [p for p in mine if p not in theirs])
        else:
            out[key] = value
    return out


def adopt_login_env() -> list[str]:
    """When started by the desktop app, take on the login shell's environment. Returns
    the names of the variables that changed (for the log; values may be secrets)."""
    if os.environ.get("REVIEWR_GUI") != "1":
        return []
    shell = read_login_env()
    if shell is None:
        return []
    merged = merge(dict(os.environ), shell)
    changed = sorted(k for k, v in merged.items() if os.environ.get(k) != v)
    os.environ.update(merged)
    return changed
