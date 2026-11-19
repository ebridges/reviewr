"""Which AI provider reviewr uses: Claude (Anthropic SDK and Claude Agent SDK) or Codex
(the OpenAI Codex CLI, signed in with `codex login`).

The choice applies to every AI step: decision suggestions, reply drafts, work-item
planning, patch checks, and the coding agent. It's saved in <data_dir>/settings.json so
it survives restarts; `[ai] provider` in reviewr.toml sets the default.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Literal

from reviewr.config import load_config

Provider = Literal["claude", "codex"]
PROVIDERS: tuple[Provider, ...] = ("claude", "codex")


def _settings_path():
    return load_config().data_dir / "settings.json"


def _read() -> dict:
    p = _settings_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def current() -> Provider:
    value = _read().get("ai_provider") or load_config().ai.provider
    return value if value in PROVIDERS else "claude"


def set_current(provider: Provider) -> None:
    data = _read()
    data["ai_provider"] = provider
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


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
