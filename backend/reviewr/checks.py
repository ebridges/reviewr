"""Lint and tests for one work item, scoped to the files it changes.

For each configured check whose `match` globs cover any changed file:
- lint checks run on those files only;
- test checks run only the tests found for those files (end-to-end suites are always
  excluded via `exclude_tests`), and are skipped when no tests are found.
"""

from __future__ import annotations

import re
import subprocess
import time
from datetime import UTC, datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

from reviewr import envs
from reviewr.config import CheckConfig, Config, RepoConfig

OUTPUT_TAIL = 6000


class ChangedPath(BaseModel):
    path: str
    deleted: bool = False


class CheckRun(BaseModel):
    name: str
    kind: str
    status: Literal["passed", "failed", "skipped", "error"]
    reason: str = ""
    command: str = ""
    files: list[str] = Field(default_factory=list)
    tests: list[str] = Field(default_factory=list)
    output: str = ""
    duration_s: float = 0.0


class CheckReport(BaseModel):
    ran_at: datetime
    scope: list[str]
    runs: list[CheckRun]

    @property
    def failed(self) -> list[CheckRun]:
        return [r for r in self.runs if r.status in ("failed", "error")]


_DIFF_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$")


def changed_paths(patch: str) -> list[ChangedPath]:
    """Files a unified diff touches, marking deletions."""
    out: list[ChangedPath] = []
    for line in patch.splitlines():
        if m := _DIFF_FILE.match(line):
            out.append(ChangedPath(path=m.group(2)))
        elif line.startswith("deleted file mode") and out:
            out[-1].deleted = True
    return out


def _rel(path: str, cwd: str) -> str | None:
    if cwd in ("", "."):
        return path
    prefix = cwd.rstrip("/") + "/"
    return path[len(prefix):] if path.startswith(prefix) else None


def _fill(argv: list[str], clone: Path, files: list[str], tests: list[str]) -> list[str]:
    out: list[str] = []
    for part in argv:
        if part == "{files}":
            out.extend(files)
        elif part == "{tests}":
            out.extend(tests)
        else:
            out.append(part.replace("{clone}", str(clone)))
    return out


def _excluded(path_from_root: str, patterns: list[str]) -> bool:
    return any(fnmatch(path_from_root, p) for p in patterns)


def _python_tests(cwd: Path, files: list[ChangedPath]) -> list[str]:
    """Test files (relative to cwd) that import a changed module or are named after it."""
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard",
         ":(glob)**/test_*.py", ":(glob)**/*_test.py"],
        cwd=cwd, capture_output=True, text=True, check=False,
    ).stdout.splitlines()
    found: set[str] = set()
    for f in files:
        p = PurePosixPath(f.path)
        if p.suffix != ".py":
            continue
        if p.name.startswith("test_") or p.stem.endswith("_test"):
            if not f.deleted:
                found.add(f.path)
            continue
        stem = p.stem
        if stem not in ("__init__", "conftest"):
            found.update(t for t in listed if PurePosixPath(t).stem in (f"test_{stem}", f"{stem}_test"))
        module = ".".join(p.with_suffix("").parts).removesuffix(".__init__")
        parent, _, leaf = module.rpartition(".")
        patterns = ["-e", module]
        if parent:
            patterns += ["-e", f"from {parent} import {leaf}"]
        grep = subprocess.run(
            ["git", "grep", "-l", "-F", *patterns, "--", ":(glob)**/test_*.py", ":(glob)**/*_test.py"],
            cwd=cwd, capture_output=True, text=True, check=False,
        ).stdout.splitlines()
        found.update(grep)
    return sorted(found)


def _run(argv: list[str], cwd: Path, timeout: int, full: bool = False, env: dict[str, str] | None = None) -> tuple[int, str, float]:
    """Run a command. Output is trimmed to its last part unless `full` (discovery
    output is parsed line by line, so it must not be cut)."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False, env=env,
        )
        output = proc.stdout if full else proc.stdout + proc.stderr
        code = proc.returncode
    except subprocess.TimeoutExpired:
        output, code = f"Timed out after {timeout} seconds.", -1
    except FileNotFoundError as e:
        output, code = f"Command not found: {e.filename}", -2
    return code, output if full else output[-OUTPUT_TAIL:], time.monotonic() - start


# pytest exit codes: 2 interrupted (e.g. import errors during collection), 3 internal
# error, 4 usage error. These mean the tests couldn't run, not that they failed.
_PYTEST_COULD_NOT_RUN = {
    2: "Tests couldn't start (import or collection error). Check the test environment in your clone.",
    3: "pytest hit an internal error.",
    4: "Tests couldn't start (import error or bad arguments). Check the test environment in your clone.",
}

# Result lines worth showing on their own, e.g. "Tests: 2 failed, 310 passed" (jest) or
# "3 failed, 41 passed in 2.1s" (pytest).
_SUMMARY = re.compile(r"^(Tests:|Test Suites:|=+ .*\b(passed|failed|error)\b.* =+$|\d+ (passed|failed)\b|Found \d+ error)")


def _summary(output: str) -> str:
    lines = [ln.strip() for ln in output.splitlines() if _SUMMARY.match(ln.strip())]
    return " · ".join(dict.fromkeys(lines))[:300]


def _one(
    check: CheckConfig, clone: Path, worktree: Path, repo: RepoConfig, changed: list[ChangedPath],
    proc_env: dict[str, str] | None = None,
) -> CheckRun:
    in_scope = [c for c in changed if any(fnmatch(c.path, g) for g in check.match)]
    base = CheckRun(name=check.name, kind=check.kind, status="skipped")
    if not in_scope:
        return base.model_copy(update={"reason": "No changed files of this type."})
    cwd = worktree / check.cwd
    files = [r for c in in_scope if not c.deleted and (r := _rel(c.path, check.cwd)) is not None]
    rel_changed = [ChangedPath(path=r, deleted=c.deleted) for c in in_scope if (r := _rel(c.path, check.cwd)) is not None]

    tests: list[str] = []
    if check.kind == "test":
        if check.discover == "python":
            tests = _python_tests(cwd, rel_changed)
        elif isinstance(check.discover, list):
            code, out, _ = _run(_fill(check.discover, clone, files, []), cwd, check.timeout, full=True, env=proc_env)
            if code != 0:
                return base.model_copy(update={"status": "error", "reason": "Finding tests failed.", "output": out[-OUTPUT_TAIL:]})
            tests = [_relative_to(line.strip(), cwd) for line in out.splitlines() if line.strip()]
        prefix = "" if check.cwd in ("", ".") else check.cwd.rstrip("/") + "/"
        tests = [t for t in tests if not _excluded(prefix + t, repo.exclude_tests)]
        if not tests:
            return base.model_copy(update={"reason": "No tests found for these files.", "files": files})
        if len(tests) > check.max_tests:
            return base.model_copy(update={
                "reason": f"{len(tests)} test files relate to these files, more than the limit of {check.max_tests} "
                          "(max_tests in reviewr.toml). That's close to a full run; leave it to CI.",
                "files": files, "tests": tests,
            })
    elif not files:
        return base.model_copy(update={"reason": "Only deleted files; nothing to lint."})

    argv = _fill(check.run, clone, files, tests)
    code, output, secs = _run(argv, cwd, check.timeout, env=proc_env)
    is_pytest = "pytest" in " ".join(argv)
    reason = ""
    if is_pytest and code == 5:
        status, reason = "skipped", "pytest collected no tests."
    elif is_pytest and code in _PYTEST_COULD_NOT_RUN:
        status, reason = "error", _PYTEST_COULD_NOT_RUN[code]
    elif code == -1:
        status, reason = "error", "Timed out."
    elif code == -2:
        status, reason = "error", "Command not found."
    else:
        status = "passed" if code == 0 else "failed"
    return CheckRun(
        name=check.name, kind=check.kind, status=status, reason=reason or _summary(output),
        command=" ".join(argv[:6]) + (" …" if len(argv) > 6 else ""),
        files=files, tests=tests, output=output, duration_s=round(secs, 1),
    )


def _relative_to(path: str, cwd: Path) -> str:
    """Make an absolute path from a discovery tool relative to cwd. Compares resolved
    paths, since /tmp and /private/tmp (and similar) name the same place."""
    if not path.startswith("/"):
        return path
    try:
        return str(Path(path).resolve().relative_to(cwd.resolve()))
    except ValueError:
        return path


def run_checks(cfg: Config, slug: str, worktree: Path, patch: str) -> CheckReport:
    """Install the branch's dependencies if needed, link them in, and run the checks
    that apply to the files `patch` changes."""
    repo = cfg.repos[slug]
    changed = changed_paths(patch)
    links = envs.link_envs(cfg, slug, worktree)
    proc_env = envs.process_env(repo)
    try:
        runs = [_one(c, repo.path, worktree, repo, changed, proc_env) for c in repo.checks]
    finally:
        envs.unlink(links)
    return CheckReport(ran_at=datetime.now(UTC), scope=[c.path for c in changed], runs=runs)


def failure_summary(report: CheckReport, limit: int = 3000) -> str:
    parts = [f"{r.name} {r.status}:\n{r.output[-limit // max(1, len(report.failed)):]}" for r in report.failed]
    return "\n\n".join(parts)
