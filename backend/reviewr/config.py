"""Load reviewr.toml.

Search order: $REVIEWR_CONFIG, ./reviewr.toml, ../reviewr.toml, ~/.config/reviewr/config.toml.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

# {root}: the worktree, opened as the project (an already-open window for it is reused);
# {path}, {line}: the file inside it.
DEFAULT_EDITORS: dict[str, list[str]] = {
    "vscode": ["code", "{root}", "--goto", "{path}:{line}"],
    "cursor": ["cursor", "{root}", "--goto", "{path}:{line}"],
    "pycharm": ["pycharm", "{root}", "--line", "{line}", "{path}"],
}


class CheckConfig(BaseModel):
    """One lint or test command, run on a work item's files only.

    `run` and `discover` are argv lists. Placeholders:
      {clone}  absolute path of your clone (for tools installed there)
      {files}  the work item's changed files that match `match`, relative to `cwd`
      {tests}  test files found for those files, relative to `cwd`
    """

    name: str
    kind: Literal["lint", "test"]
    match: list[str] = Field(description="fnmatch globs, relative to the repo root")
    cwd: str = "."
    run: list[str]
    # How to find tests for a "test" check: "python" uses reviewr's import and name
    # matching; a command (e.g. jest --listTests --findRelatedTests {files}) prints one
    # test path per line.
    discover: str | list[str] | None = None
    timeout: int = 600
    # A file imported almost everywhere (a package __init__, generated API types) relates
    # to most of the suite. Above this many test files the check is skipped with a note,
    # since that's a full run better left to CI.
    max_tests: int = 60


class EnvConfig(BaseModel):
    """Dependencies installed from the branch's own lockfiles (see envs.py)."""

    name: str
    # Repo-relative files the install needs; their contents decide when to reinstall.
    files: list[str]
    # argv lists, run in the env folder (which holds copies of `files`). {env} is its path.
    install: list[list[str]]
    # worktree path -> path inside the env folder, linked in while drafts and checks run.
    provides: dict[str, str]
    # Paths to copy (copy-on-write) from the most recent earlier install before running
    # `install_seeded`, so a lockfile change only installs the differences.
    seed: list[str] = Field(default_factory=list)
    install_seeded: list[list[str]] | None = None
    timeout: int = 1800


class RepoConfig(BaseModel):
    path: Path
    # Dependency environments built from the branch under review, not your clone.
    envs: list[EnvConfig] = Field(default_factory=list)
    # Put before PATH for installs and checks (e.g. the Node version the repo needs).
    path_prepend: list[str] = Field(default_factory=list)
    # Test files matching these globs are never run per work item (e2e suites).
    exclude_tests: list[str] = Field(default_factory=lambda: ["*e2e*", "*playwright*", "*cypress*"])
    checks: list[CheckConfig] = Field(default_factory=list)

    def model_post_init(self, _ctx) -> None:
        self.path = self.path.expanduser()


class JiraConfig(BaseModel):
    base_url: str  # e.g. https://yourcompany.atlassian.net
    project: str  # project key, e.g. MOS
    issue_type: str = "Task"
    email: str | None = None  # falls back to $JIRA_EMAIL; the token is $JIRA_API_TOKEN


class LLMConfig(BaseModel):
    model: str = "claude-opus-5-5"
    effort: str = "medium"


class AIConfig(BaseModel):
    provider: str = "claude"  # "claude" or "codex"; the app's switcher overrides it
    codex_model: str | None = None  # None: the Codex CLI's own default


class Config(BaseModel):
    default_editor: str = "vscode"
    # Each editor is an argv list, not a shell string, so file paths from PR data are
    # never interpreted by a shell. Placeholders: {root} (worktree folder), {path} (absolute), {line}.
    editors: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_EDITORS))
    repos: dict[str, RepoConfig] = Field(default_factory=dict)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    jira: JiraConfig | None = None
    data_dir: Path = Path("~/.local/share/reviewr")

    def model_post_init(self, _ctx) -> None:
        self.data_dir = self.data_dir.expanduser()

    def repo_path(self, slug: str) -> Path:
        try:
            return self.repos[slug].path
        except KeyError:
            raise LookupError(
                f"No local clone configured for {slug}. Add this to reviewr.toml:\n\n"
                f'[repos."{slug}"]\npath = "~/path/to/clone"'
            ) from None


def _find_config_file() -> Path | None:
    candidates = [
        os.environ.get("REVIEWR_CONFIG"),
        "reviewr.toml",
        "../reviewr.toml",
        "~/.config/reviewr/config.toml",
    ]
    for c in candidates:
        if c and (p := Path(c).expanduser()).is_file():
            return p
    return None


# Keys that came from .env rather than the shell, so Settings can say where a
# credential came from.
DOTENV_KEYS: set[str] = set()


def load_dotenv() -> None:
    """Load KEY=VALUE lines from the project's .env without overriding the environment."""
    for candidate in (Path(".env"), Path("../.env")):
        if candidate.is_file():
            for line in candidate.read_text().splitlines():
                line = line.strip().removeprefix("export ").strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if key not in os.environ:
                    os.environ[key] = value.strip().strip("'\"")
                    DOTENV_KEYS.add(key)
            return


@lru_cache
def load_config() -> Config:
    path = _find_config_file()
    if path is None:
        return Config()
    raw = tomllib.loads(path.read_text())
    editors = {**DEFAULT_EDITORS, **raw.get("editors", {})}
    return Config(
        default_editor=raw.get("editor", {}).get("default", "vscode"),
        editors=editors,
        repos=raw.get("repos", {}),
        llm=raw.get("llm", {}),
        ai=raw.get("ai", {}),
        jira=raw.get("jira"),
        **({"data_dir": raw["data_dir"]} if "data_dir" in raw else {}),
    )
