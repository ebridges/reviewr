"""reviewr's settings: what the Settings page edits.

They're saved as JSON in ~/.config/reviewr/settings.json ($REVIEWR_SETTINGS overrides the
path, for tests). Secrets (the Anthropic API key, the Jira API token) aren't in this
file; they're in the macOS Keychain (see secrets.py).

reviewr.toml, which held these settings before, is imported once: if there's no
settings file yet, the first reviewr.toml found is read, saved as settings.json, and
renamed to reviewr.toml.imported. The provider choice and API tokens kept elsewhere
then are imported at the same time (see _import_legacy).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# The project folder (backend/reviewr/config.py -> ../../). In the packaged app this is
# inside the bundle.
_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path("~/.config/reviewr").expanduser()

# {root}: the worktree, opened as the project (an already-open window for it is reused);
# {path}, {line}: the file inside it.
DEFAULT_EDITORS: dict[str, list[str]] = {
    "vscode": ["code", "{root}", "--goto", "{path}:{line}"],
    "cursor": ["cursor", "{root}", "--goto", "{path}:{line}"],
    "pycharm": ["pycharm", "{root}", "--line", "{line}", "{path}"],
}

# The AI steps guidance can be written for (see guidance.py).
GUIDANCE_STEPS = ("suggest", "replies", "plan", "check", "agent", "conflicts", "ci")


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
    # Toolchain versions workspace setup found (e.g. {"node": "22.11.0"}), for display.
    toolchain: dict[str, str] = Field(default_factory=dict)
    # Guidance for this repo's AI steps, added after the global guidance (step -> text).
    guidance: dict[str, str] = Field(default_factory=dict)

    def model_post_init(self, _ctx) -> None:
        self.path = self.path.expanduser()


class JiraConfig(BaseModel):
    base_url: str  # e.g. https://yourcompany.atlassian.net
    project: str  # project key, e.g. MOS
    issue_type: str = "Task"
    email: str | None = None  # the API token is in the Keychain


class LLMConfig(BaseModel):
    model: str = "claude-opus-5-5"
    effort: str = "medium"


class AIConfig(BaseModel):
    provider: Literal["claude", "codex"] = "claude"  # the header switch changes it
    codex_model: str | None = None  # None: the Codex CLI's own default
    # How reviewr signs in to the Claude API: "ant" uses the `ant auth login` profile;
    # "api_key" uses the key saved in the Keychain.
    claude_auth: Literal["ant", "api_key"] = "ant"


class Config(BaseModel):
    default_editor: str = "vscode"
    # Each editor is an argv list, not a shell string, so file paths from PR data are
    # never interpreted by a shell. Placeholders: {root} (worktree folder), {path} (absolute), {line}.
    editors: dict[str, list[str]] = Field(default_factory=lambda: dict(DEFAULT_EDITORS))
    # Where workspace setup clones repos: <workspace_dir>/<owner>/<repo>.
    workspace_dir: Path = Path("~/Projects")
    repos: dict[str, RepoConfig] = Field(default_factory=dict)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    jira: JiraConfig | None = None
    # Guidance for every repo's AI steps (step -> text), after reviewr's built-in guidance.
    guidance: dict[str, str] = Field(default_factory=dict)
    data_dir: Path = Path("~/.local/share/reviewr")

    def model_post_init(self, _ctx) -> None:
        self.data_dir = self.data_dir.expanduser()
        self.workspace_dir = self.workspace_dir.expanduser()

    def repo_path(self, slug: str) -> Path:
        try:
            return self.repos[slug].path
        except KeyError:
            raise LookupError(
                f"{slug} isn't set up yet. Open one of its pull requests to set up the workspace, "
                "or add its clone under Settings → Workspace."
            ) from None


def settings_path() -> Path:
    return Path(os.environ.get("REVIEWR_SETTINGS") or CONFIG_DIR / "settings.json").expanduser()


_save_lock = threading.Lock()


@lru_cache
def load_config() -> Config:
    path = settings_path()
    if path.is_file():
        try:
            return Config.model_validate_json(path.read_text())
        except ValueError:
            log.exception("Couldn't read %s; using default settings", path)
            return Config()
    if os.environ.get("REVIEWR_SETTINGS"):
        return Config()  # tests: never import the machine's real config
    cfg = _import_legacy()
    if cfg is not None:
        save_config(cfg)
    return cfg or Config()


def save_config(cfg: Config) -> Config:
    """Write the settings (atomically) and make load_config return them."""
    path = settings_path()
    with _save_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = cfg.model_dump(mode="json")
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as f:
            json.dump(data, f, indent=2)
            tmp = Path(f.name)
        tmp.replace(path)
        load_config.cache_clear()
    return load_config()


def update_config(change) -> Config:
    """Load the settings, apply `change(cfg)` to a copy, validate and save them."""
    cfg = load_config().model_copy(deep=True)
    change(cfg)
    return save_config(Config.model_validate(cfg.model_dump()))


# Importing the old configuration (reviewr.toml, .env, data_dir/settings.json)

def _legacy_toml() -> Path | None:
    candidates = [
        os.environ.get("REVIEWR_CONFIG"),
        "reviewr.toml",
        "../reviewr.toml",
        str(_ROOT / "reviewr.toml"),
        str(CONFIG_DIR / "config.toml"),
    ]
    for c in candidates:
        if c and (p := Path(c).expanduser()).is_file():
            return p
    return None


def _legacy_dotenv() -> dict[str, str]:
    for candidate in (Path(".env"), Path("../.env"), _ROOT / ".env", CONFIG_DIR / ".env"):
        if candidate.is_file():
            out = {}
            for line in candidate.read_text().splitlines():
                line = line.strip().removeprefix("export ").strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    out[key.strip()] = value.strip().strip("'\"")
            return out
    return {}


def _import_legacy() -> Config | None:
    """Settings from reviewr.toml and .env, or None if there's neither."""
    toml_path, dotenv = _legacy_toml(), _legacy_dotenv()
    if toml_path is None and not dotenv:
        return None
    cfg = Config()
    if toml_path is not None:
        raw = tomllib.loads(toml_path.read_text())
        cfg = Config(
            default_editor=raw.get("editor", {}).get("default", "vscode"),
            editors={**DEFAULT_EDITORS, **raw.get("editors", {})},
            repos=raw.get("repos", {}),
            llm=raw.get("llm", {}),
            ai={k: v for k, v in raw.get("ai", {}).items() if k in ("provider", "codex_model")},
            jira=raw.get("jira"),
            **({"data_dir": raw["data_dir"]} if "data_dir" in raw else {}),
        )
    old_settings = cfg.data_dir / "settings.json"
    try:
        provider = json.loads(old_settings.read_text()).get("ai_provider") if old_settings.is_file() else None
    except (OSError, ValueError):
        provider = None
    if provider in ("claude", "codex"):
        cfg.ai.provider = provider

    from reviewr import secrets  # here to avoid an import cycle

    if key := dotenv.get("ANTHROPIC_API_KEY"):
        secrets.set_secret(secrets.ANTHROPIC_API_KEY, key)
        cfg.ai.claude_auth = "api_key"
    if token := dotenv.get("JIRA_API_TOKEN"):
        secrets.set_secret(secrets.JIRA_API_TOKEN, token)
    if cfg.jira and not cfg.jira.email and dotenv.get("JIRA_EMAIL"):
        cfg.jira.email = dotenv["JIRA_EMAIL"]

    if toml_path is not None:
        try:
            toml_path.rename(toml_path.with_name(toml_path.name + ".imported"))
        except OSError:
            log.warning("Imported %s but couldn't rename it", toml_path)
    log.info("Imported settings from %s", toml_path or ".env")
    return cfg
