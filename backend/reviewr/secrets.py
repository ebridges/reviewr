"""Secrets reviewr keeps: the Anthropic API key and the Jira API token.

They're stored in the macOS Keychain (or the platform's keyring) under the service
"reviewr", never in settings.json or a dotfile. Where no keyring is available, they go in
~/.config/reviewr/secrets.json, readable only by you. $REVIEWR_SECRETS_FILE forces the
file (used by the tests).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SERVICE = "reviewr"
ANTHROPIC_API_KEY = "anthropic_api_key"
JIRA_API_TOKEN = "jira_api_token"
NAMES = (ANTHROPIC_API_KEY, JIRA_API_TOKEN)


def _file() -> Path | None:
    """The secrets file, when the keyring isn't used."""
    if forced := os.environ.get("REVIEWR_SECRETS_FILE"):
        return Path(forced)
    try:
        import keyring
        from keyring.backends import fail

        if not isinstance(keyring.get_keyring(), fail.Keyring):
            return None
    except (ImportError, RuntimeError) as e:  # no keyring module or backend
        log.debug("No keyring available: %s", e)
    from reviewr.config import CONFIG_DIR

    return CONFIG_DIR / "secrets.json"


def _read_file(path: Path) -> dict[str, str]:
    try:
        return json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def get_secret(name: str) -> str | None:
    if (path := _file()) is not None:
        return _read_file(path).get(name) or None
    import keyring

    try:
        return keyring.get_password(SERVICE, name) or None
    except Exception:
        log.exception("Couldn't read %s from the keychain", name)
        return None


def set_secret(name: str, value: str) -> None:
    value = value.strip()
    if not value:
        delete_secret(name)
        return
    if (path := _file()) is not None:
        data = _read_file(path)
        data[name] = value
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(json.dumps(data, indent=2))
        return
    import keyring

    keyring.set_password(SERVICE, name, value)


def delete_secret(name: str) -> None:
    if (path := _file()) is not None:
        data = _read_file(path)
        if data.pop(name, None) is not None:
            path.write_text(json.dumps(data, indent=2))
        return
    import keyring
    from keyring.errors import PasswordDeleteError

    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        pass


def has_secret(name: str) -> bool:
    return get_secret(name) is not None
