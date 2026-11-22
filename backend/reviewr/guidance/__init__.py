"""Guidance: instructions added to the system prompt of each AI step, so you can steer
how reviewr suggests decisions, drafts replies, plans, checks patches and edits code.

For a step (see config.GUIDANCE_STEPS), the text is assembled from, in order:
  1. reviewr's built-in guidance for the step (<step>.md in this folder)
  2. your guidance for every repo (Settings → Guidance)
  3. your guidance for this repo (Settings → Workspace → the repo)
  4. guidance committed in the repo: .reviewr/guidance/all.md and .reviewr/guidance/<step>.md
  5. the repo's own conventions for coding steps: CLAUDE.md or AGENTS.md at its root
and, for the coding agent, a list of the repo's skills (.claude/skills/*/SKILL.md) that
it can open with its editor tool when one applies.
"""

from __future__ import annotations

import re
from pathlib import Path

from reviewr.config import GUIDANCE_STEPS, Config, load_config

_BUILTIN = Path(__file__).resolve().parent  # the .md files beside this module
_MAX_FILE_CHARS = 20_000

STEP_LABELS = {
    "suggest": "Decision suggestions",
    "replies": "Reply drafts",
    "plan": "Work-item planning",
    "check": "Patch checks",
    "agent": "Coding agent",
    "conflicts": "Merge conflicts",
    "ci": "CI failures",
}
# Steps that work on code, where the repo's CLAUDE.md / AGENTS.md conventions apply.
_CODE_STEPS = {"agent", "plan", "check", "conflicts", "ci"}
_CONVENTIONS = ("CLAUDE.md", "AGENTS.md")


def builtin(step: str) -> str:
    p = _BUILTIN / f"{step}.md"
    return p.read_text().strip() if p.is_file() else ""


def _read(p: Path) -> str:
    try:
        text = p.read_text(errors="replace").strip()
    except OSError:
        return ""
    return text[:_MAX_FILE_CHARS] + ("\n[…truncated]" if len(text) > _MAX_FILE_CHARS else "")


def repo_files(root: Path, step: str) -> list[tuple[str, str]]:
    """(label, text) for guidance and conventions committed in the repo."""
    out = []
    for name in ("all.md", f"{step}.md"):
        if text := _read(root / ".reviewr" / "guidance" / name):
            out.append((f".reviewr/guidance/{name}", text))
    if step in _CODE_STEPS:
        for name in _CONVENTIONS:
            if text := _read(root / name):
                out.append((name, text))
                break
    return out


_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def skills_index(root: Path) -> list[tuple[str, str, str]]:
    """(name, description, path) for each skill in the repo's .claude/skills."""
    out = []
    for skill in sorted((root / ".claude" / "skills").glob("*/SKILL.md")):
        meta = {}
        if m := _FRONTMATTER.match(_read(skill)):
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip("'\"")
        out.append((meta.get("name", skill.parent.name), meta.get("description", ""), str(skill.relative_to(root))))
    return out


def for_step(step: str, slug: str | None = None, root: Path | None = None, cfg: Config | None = None) -> str:
    """The guidance text to append to a step's system prompt ("" if there's none).

    `root` is where to read the repo's own files: the draft worktree for the agent;
    otherwise your clone of the repo."""
    assert step in GUIDANCE_STEPS, step
    cfg = cfg or load_config()
    repo = cfg.repos.get(slug) if slug else None
    if root is None and repo is not None:
        root = repo.path
    parts: list[str] = []
    if text := builtin(step):
        parts.append(text)
    if text := cfg.guidance.get(step, "").strip():
        parts.append(text)
    if repo and (text := repo.guidance.get(step, "").strip()):
        parts.append(text)
    if root is not None and root.is_dir():
        for label, text in repo_files(root, step):
            parts.append(f"From the repository's {label}:\n\n{text}")
        if step == "agent" and (skills := skills_index(root)):
            lines = [f"- {name}: {desc} ({path})" for name, desc, path in skills]
            parts.append(
                "The repository has these skills, written for coding agents. When one applies to "
                "what you're doing, read its file with the editor tool and follow it:\n" + "\n".join(lines)
            )
    if not parts:
        return ""
    return "<guidance>\n" + "\n\n".join(parts) + "\n</guidance>"


def with_guidance(system: str, step: str, slug: str | None = None, root: Path | None = None) -> str:
    extra = for_step(step, slug, root)
    return f"{system}\n\n{extra}" if extra else system
