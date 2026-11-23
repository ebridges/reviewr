"""The app's environment when started from Finder, editor lookup, signing failures, and
applying a draft without committing it."""

import shutil
import subprocess
from pathlib import Path

import pytest

from reviewr import editor, fixwork, git, shellenv
from tests.test_merge import setup, sh  # noqa: F401  (setup is a fixture)


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


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_signing_failure_is_not_blamed_on_the_hooks(setup, monkeypatch, tmp_path):  # noqa: F811
    cfg, pr, clone = setup
    key = tmp_path / "signing"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    sh(clone, "config", "commit.gpgsign", "true")
    sh(clone, "config", "gpg.format", "ssh")
    sh(clone, "config", "user.signingkey", (tmp_path / "signing.pub").read_text().strip())
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "no-agent.sock"))  # what a Finder-started app had
    (clone / "app.py").write_text((clone / "app.py").read_text() + "X = 2\n")
    sh(clone, "add", "app.py")
    head = sh(clone, "rev-parse", "HEAD")
    with pytest.raises(git.GitError) as e:
        fixwork.commit_with_hooks(cfg, pr.ref.slug, clone, "m", ["app.py"])
    msg = str(e.value)
    assert msg.startswith("Your commit hooks passed, but git couldn't sign the commit")
    assert "agent" in msg and "commit hooks failed" not in msg
    assert sh(clone, "rev-parse", "HEAD") == head


def test_apply_without_committing(setup, tmp_path):  # noqa: F811
    from reviewr.state import WorkItem
    from reviewr.store import Store

    cfg, pr, clone = setup
    original = (clone / "app.py").read_text()
    (clone / "app.py").write_text(original.replace("'", '"'))
    patch = sh(clone, "diff")
    (clone / "app.py").write_text(original)
    sh(clone, "status", "--porcelain")  # refresh the index after rewriting the file
    st = Store(tmp_path / "s.sqlite3")
    st.update_state(pr.ref, lambda s: s.plan.append(WorkItem(id=1, title="Quotes", thread_ids=[], status="ready", patch=patch)))
    head = sh(clone, "rev-parse", "HEAD")

    state = fixwork.apply_uncommitted(st, cfg, pr, 1)
    item = state.item(1)
    assert item.mode == "me" and item.status == "pending"
    assert "'" not in (clone / "app.py").read_text()  # the change is in your checkout
    assert sh(clone, "rev-parse", "HEAD") == head and "app.py" in sh(clone, "status", "--porcelain")  # uncommitted

    st.update_state(pr.ref, lambda s: setattr(s.item(1), "status", "ready"))
    with pytest.raises(ValueError, match="doesn't apply"):
        fixwork.apply_uncommitted(st, cfg, pr, 1)  # already applied: refused, file left alone
    assert "'" not in (clone / "app.py").read_text()


def test_bundled_paths_are_relative():
    assert all(not Path(p).is_absolute() for paths in editor._BUNDLED.values() for p in paths)
