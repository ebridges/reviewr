"""Which AI provider reviewr uses: Claude (the Anthropic API) or Codex (the OpenAI Codex
CLI, signed in with `codex login`).

The choice applies to every AI step: decision suggestions, reply drafts, work-item
planning, patch checks, and the coding agent. It's part of the settings (ai.provider);
the Claude / Codex switch in the header and Settings both change it.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Literal

from reviewr.config import load_config, update_config

Provider = Literal["claude", "codex"]
PROVIDERS: tuple[Provider, ...] = ("claude", "codex")


def current() -> Provider:
    return load_config().ai.provider


def set_current(provider: Provider) -> None:
    update_config(lambda c: setattr(c.ai, "provider", provider))


def codex_ready() -> tuple[bool, str | None]:
    """Whether the Codex CLI is installed and signed in, and its status line."""
    if not shutil.which("codex"):
        return False, None
    try:
        proc = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        return False, None
    line = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode == 0, (line[0] if line else None)
