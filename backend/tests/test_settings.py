import json
import subprocess
from pathlib import Path

import pytest

from reviewr import config, guidance, secrets
from reviewr.agent_tools import ToolError, Workspace
from reviewr.config import Config, RepoConfig, load_config, save_config, update_config


def _git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
    return root


# Agent tools

def test_workspace_paths_stay_inside_the_repo(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "a.py").write_text("x = 1\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("no\n")
    (repo / "deps").symlink_to(tmp_path)  # like a linked node_modules
    ws = Workspace(repo)
    for bad in ("../secret.txt", str(outside), "deps/secret.txt", ".git/config"):
        with pytest.raises(ToolError):
            ws.editor({"command": "view", "path": bad})
    with pytest.raises(ToolError):
        ws.editor({"command": "create", "path": "deps/new.py", "file_text": "x"})
    assert ws.editor({"command": "view", "path": "a.py"}) == "1\tx = 1"


def test_workspace_editing(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "a.py").write_text("x = 1\ny = 2\n")
    ws = Workspace(repo)
    ws.editor({"command": "str_replace", "path": "a.py", "old_str": "y = 2", "new_str": "y = 3"})
    ws.editor({"command": "insert", "path": "a.py", "insert_line": 0, "insert_text": "# top"})
    ws.editor({"command": "create", "path": "pkg/b.py", "file_text": "z = 1\n"})
    assert (repo / "a.py").read_text() == "# top\nx = 1\ny = 3\n"
    assert (repo / "pkg" / "b.py").read_text() == "z = 1\n"
    assert ws.changed == {"a.py", "pkg/b.py"}
    with pytest.raises(ToolError, match="wasn't found"):
        ws.editor({"command": "str_replace", "path": "a.py", "old_str": "nope", "new_str": ""})
    (repo / "c.py").write_text("q\nq\n")
    with pytest.raises(ToolError, match="2 times"):
        ws.editor({"command": "str_replace", "path": "c.py", "old_str": "q", "new_str": "r"})
    assert ws.editor({"command": "view", "path": "a.py", "view_range": [2, -1]}) == "2\tx = 1\n3\ty = 3"


def test_workspace_find_and_search(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("export const answer = 42;\n")
    (repo / "README.md").write_text("answer here\n")
    (repo / ".gitignore").write_text("build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "out.ts").write_text("answer\n")
    ws = Workspace(repo)
    assert ws.find("*.ts") == "src/app.ts"  # ignored build/ isn't listed
    assert ws.search("answer", "src/**") == "src/app.ts:1:export const answer = 42;"
    assert "README.md:1:answer here" in ws.search("answer", "")
    assert ws.search("nothing-matches-this", "") == "No matches."


# Guidance

def test_guidance_is_assembled_in_order(tmp_path: Path):
    repo = _git_repo(tmp_path / "repo")
    (repo / ".reviewr" / "guidance").mkdir(parents=True)
    (repo / ".reviewr" / "guidance" / "agent.md").write_text("Committed agent guidance.")
    (repo / ".reviewr" / "guidance" / "all.md").write_text("Committed guidance for every step.")
    (repo / "CLAUDE.md").write_text("Use tabs.")
    (repo / ".claude" / "skills" / "migrations").mkdir(parents=True)
    (repo / ".claude" / "skills" / "migrations" / "SKILL.md").write_text(
        "---\nname: migrations\ndescription: How to write a database migration\n---\nSteps…"
    )
    cfg = Config(guidance={"agent": "Global agent guidance."},
                 repos={"o/r": RepoConfig(path=repo, guidance={"agent": "Repo agent guidance."})})
    text = guidance.for_step("agent", "o/r", cfg=cfg)
    order = [guidance.builtin("agent")[:30], "Global agent guidance.", "Repo agent guidance.",
             "Committed guidance for every step.", "Committed agent guidance.", "Use tabs.", "migrations: How to write"]
    positions = [text.index(part) for part in order]
    assert positions == sorted(positions)
    # Replies don't get the coding conventions or the skills.
    replies = guidance.for_step("replies", "o/r", cfg=cfg)
    assert "Use tabs." not in replies and "migrations" not in replies


# Settings store and the reviewr.toml import

def test_settings_round_trip():
    update_config(lambda c: setattr(c, "workspace_dir", Path("/tmp/ws")))
    assert load_config().workspace_dir == Path("/tmp/ws")
    saved = json.loads(config.settings_path().read_text())
    assert saved["workspace_dir"] == "/tmp/ws"


def test_reviewr_toml_and_env_are_imported_once(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("REVIEWR_SETTINGS")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "cfg")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "reviewr.toml").write_text(
        f'[llm]\nmodel = "claude-sonnet-5-5"\n[repos."o/r"]\npath = "{tmp_path}/clone"\n'
        f'[jira]\nbase_url = "https://x.atlassian.net"\nproject = "P"\n'
        f'data_dir = "{tmp_path}/data"\n'
    )
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-test\nJIRA_EMAIL=me@x.com\nJIRA_API_TOKEN=tok\n")
    load_config.cache_clear()
    cfg = load_config()
    assert cfg.llm.model == "claude-sonnet-5-5" and "o/r" in cfg.repos
    assert cfg.jira and cfg.jira.email == "me@x.com" and cfg.ai.claude_auth == "api_key"
    assert secrets.get_secret(secrets.ANTHROPIC_API_KEY) == "sk-test"
    assert secrets.get_secret(secrets.JIRA_API_TOKEN) == "tok"
    assert not (tmp_path / "reviewr.toml").exists() and (tmp_path / "reviewr.toml.imported").exists()
    assert (tmp_path / "cfg" / "settings.json").is_file()
    # From now on the settings file is used.
    load_config.cache_clear()
    assert load_config().llm.model == "claude-sonnet-5-5"


def test_save_rejects_invalid_settings():
    with pytest.raises(ValueError):
        update_config(lambda c: setattr(c.ai, "provider", "gpt"))
    save_config(Config())


# The agent loop, with a stand-in for the Anthropic client

class _Block:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _FakeClient:
    """Answers the first turn with tool calls and the second with a summary."""

    def __init__(self, repo: Path):
        self.requests: list[dict] = []
        self.beta = self
        self.messages = self

    def stream(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})  # as sent
        turn = len(self.requests)
        if turn == 1:
            content = [
                _Block(type="text", text="Looking at the file."),
                _Block(type="tool_use", id="t1", name="str_replace_based_edit_tool",
                       input={"command": "str_replace", "path": "a.py", "old_str": "x = 1", "new_str": "x = 2"}),
                _Block(type="tool_use", id="t2", name="str_replace_based_edit_tool",
                       input={"command": "view", "path": "../outside.txt"}),
            ]
            reply = _Block(content=content, stop_reason="tool_use")
        else:
            reply = _Block(content=[_Block(type="text", text="Changed x to 2.")], stop_reason="end_turn")

        class _Stream:
            def __enter__(self_):
                return self_

            def __exit__(self_, *a):
                return False

            def get_final_message(self_):
                return reply

        return _Stream()


def test_agent_loop_runs_tools_and_returns_errors_to_claude(tmp_path: Path, monkeypatch):
    from reviewr import agent
    from reviewr.llm import client as llm_client

    repo = _git_repo(tmp_path / "repo")
    (repo / "a.py").write_text("x = 1\n")
    fake = _FakeClient(repo)
    monkeypatch.setattr(llm_client, "anthropic_client", lambda: fake)
    out = agent.run_agent(repo, "Change x to 2.", "claude-opus-5-5")
    assert (repo / "a.py").read_text() == "x = 2\n"
    assert "Changed x to 2." in out
    second = fake.requests[1]["messages"]
    results = second[-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]  # both results in one message
    assert results[0]["is_error"] is False and results[1]["is_error"] is True
    assert "outside the repository" in results[1]["content"]
    assert "<guidance>" in fake.requests[0]["system"]  # built-in agent guidance is included
