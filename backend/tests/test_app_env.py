"""The app's environment when started from Finder, editor lookup, signing failures, and
applying a draft without committing it."""

from pathlib import Path

import pytest

from reviewr import editor, shellenv


def test_merge_takes_the_shells_values_but_keeps_ours():
    current = {"PATH": "/opt/homebrew/bin:/usr/bin:/bin", "SSH_AUTH_SOCK": "/var/run/launchd/Listeners",
               "REVIEWR_GUI": "1", "PYTHONUNBUFFERED": "1", "HOME": "/Users/me"}
    shell = {"PATH": "/Users/me/bin:/usr/bin:/bin", "SSH_AUTH_SOCK": "/Users/me/.local/state/ssh-agent.sock",
             "REVIEWR_GUI": "0", "PYTHONUNBUFFERED": "0", "PWD": "/tmp", "SHLVL": "2", "EDITOR": "vim"}
    out = shellenv.merge(current, shell)
    assert out["PATH"] == "/Users/me/bin:/usr/bin:/bin:/opt/homebrew/bin"  # the shell's first, then ours it lacks
    assert out["SSH_AUTH_SOCK"] == "/Users/me/.local/state/ssh-agent.sock"
    assert out["REVIEWR_GUI"] == "1" and out["PYTHONUNBUFFERED"] == "1"
    assert "PWD" not in out and "SHLVL" not in out and out["EDITOR"] == "vim"


def test_read_login_env_ignores_profile_output(tmp_path):
    fake = tmp_path / "fakesh"
    # Called as `fakesh -ilc <script>`: prints like a chatty profile, then runs the script.
    fake.write_text('#!/bin/sh\necho "Welcome! (from .zprofile)"\nexport FROM_PROFILE="a b=c"\nexec /bin/sh -c "$2"\n')
    fake.chmod(0o755)
    env = shellenv.read_login_env(str(fake))
    assert env is not None and env["FROM_PROFILE"] == "a b=c"
    assert not any("Welcome" in k or "Welcome" in v for k, v in env.items())


def test_read_login_env_gives_up_quietly(tmp_path):
    assert shellenv.read_login_env(str(tmp_path / "no-such-shell")) is None
    slow = tmp_path / "slowsh"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    assert shellenv.read_login_env(str(slow), timeout=0.5) is None


def test_adopt_only_when_started_by_the_app(monkeypatch):
    monkeypatch.delenv("REVIEWR_GUI", raising=False)
    assert shellenv.adopt_login_env() == []


def test_editor_found_inside_its_app_bundle(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    code = tmp_path / "Visual Studio Code.app/Contents/Resources/app/bin/code"
    code.parent.mkdir(parents=True)
    code.write_text("#!/bin/sh\n")
    code.chmod(0o755)
    assert editor.find_command("code", [tmp_path]) == str(code)
    assert editor.find_command("nano-but-not-installed", [tmp_path]) is None


def test_bundled_paths_are_relative():
    assert all(not Path(p).is_absolute() for paths in editor._BUNDLED.values() for p in paths)
