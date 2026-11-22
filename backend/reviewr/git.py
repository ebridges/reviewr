"""Local git: diffs, stale-comment tracing, and reviewr's worktrees (D1: never the API)."""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

from reviewr.models import ChangedFile, StaleTrace, TraceCommit

SNIPPET_CONTEXT = 4


class GitError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        input=input,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def has_commit(repo: Path, oid: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{oid}^{{commit}}", check=False).returncode == 0


def ensure_commits(repo: Path, *oids: str) -> None:
    missing = [o for o in oids if not has_commit(repo, o)]
    if missing:
        raise GitError(
            f"Commits {', '.join(m[:10] for m in missing)} are not in {repo}. "
            f"Run: git -C {repo} fetch origin"
        )


def merge_base(repo: Path, base: str, head: str) -> str:
    return _git(repo, "merge-base", base, head).stdout.strip()


def changed_files(repo: Path, base: str, head: str) -> list[ChangedFile]:
    """Files changed between the merge base and head, the same set GitHub shows."""
    mb = merge_base(repo, base, head)
    status_out = _git(repo, "diff", "--name-status", "-M", "-z", mb, head).stdout
    numstat_out = _git(repo, "diff", "--numstat", "-M", "-z", mb, head).stdout

    counts: dict[str, tuple[int | None, int | None]] = {}
    parts = numstat_out.split("\0")
    i = 0
    while i < len(parts) and parts[i]:
        adds, dels, path = parts[i].split("\t")
        if path == "":  # rename: old and new paths follow as separate fields
            path = parts[i + 2]
            i += 2
        counts[path] = (None if adds == "-" else int(adds), None if dels == "-" else int(dels))
        i += 1

    files: list[ChangedFile] = []
    parts = status_out.split("\0")
    i = 0
    while i < len(parts) and parts[i]:
        status = parts[i]
        if status[0] in "RC":
            old, new = parts[i + 1], parts[i + 2]
            i += 3
        else:
            old, new = None, parts[i + 1]
            i += 2
        adds, dels = counts.get(new, (None, None))
        files.append(
            ChangedFile(path=new, old_path=old, status=status[0], additions=adds, deletions=dels)
        )
    return files


def show(repo: Path, oid: str, path: str) -> str | None:
    proc = _git(repo, "show", f"{oid}:{path}", check=False)
    return proc.stdout if proc.returncode == 0 else None


def snippet(text: str | None, line: int | None) -> tuple[str, int]:
    """A few lines around `line` (1-based), and the number of the first one."""
    if not text or not line:
        return "", 0
    lines = text.splitlines()
    start = max(1, line - SNIPPET_CONTEXT)
    end = min(len(lines), line + SNIPPET_CONTEXT)
    return "\n".join(lines[start - 1 : end]), start


# Stale comment tracing

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _renamed_to(repo: Path, old: str, new_oid: str, path: str) -> str | None:
    """Where `path` lives at `new_oid`, following a rename; None if deleted."""
    out = _git(repo, "diff", "--name-status", "-M", old, new_oid).stdout
    for row in out.splitlines():
        cols = row.split("\t")
        if cols[0] == "D" and cols[1] == path:
            return None
        if cols[0].startswith("R") and cols[1] == path:
            return cols[2]
    return path


def _map_line(repo: Path, old: str, new: str, old_path: str, new_path: str, line: int) -> tuple[int, bool]:
    """Map `line` in old:old_path to new:new_path. Returns (line, edited)."""
    diff = _git(repo, "diff", "-U0", old, new, "--", old_path, new_path).stdout
    offset = 0
    for row in diff.splitlines():
        m = _HUNK.match(row)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2) or 1)
        c, d = int(m.group(3)), int(m.group(4) or 1)
        if b == 0:  # pure insertion after old line a
            if line > a:
                offset += d
            continue
        if line < a:
            break
        if a <= line < a + b:  # the line itself was changed
            return c if d else max(1, c), True
        offset += d - b
    return line + offset, False


def trace_stale(repo: Path, path: str, original_commit: str | None, original_line: int | None, head: str) -> StaleTrace:
    """Follow a stale comment's lines from the commit it was written on to `head`."""
    empty = StaleTrace(
        status="unknown", original_commit=original_commit, original_path=path,
        original_line=original_line, current_path=None, current_line=None, commits=[],
        original_snippet="", current_snippet="", snippet_start_original=0, snippet_start_current=0,
    )
    if not original_commit or not original_line or not has_commit(repo, original_commit):
        return empty

    orig_text = show(repo, original_commit, path)
    orig_snip, orig_start = snippet(orig_text, original_line)
    current_path = _renamed_to(repo, original_commit, head, path)

    if current_path is None:
        log = _git(repo, "log", "--format=%h%x09%s", f"{original_commit}..{head}", "--", path).stdout
        return empty.model_copy(update={
            "status": "deleted",
            "commits": _commits(log),
            "original_snippet": orig_snip,
            "snippet_start_original": orig_start,
        })

    current_line, edited = _map_line(repo, original_commit, head, path, current_path, original_line)
    cur_snip, cur_start = snippet(show(repo, head, current_path), current_line)
    # File-level history. `git log -L` returns nothing when the line itself was
    # rewritten, which is the case that matters most here.
    log = _git(
        repo, "log", "--format=%h%x09%s", "-M", f"{original_commit}..{head}",
        "--", path, current_path,
    ).stdout
    if edited:
        status = "edited"
    elif current_line != original_line or current_path != path:
        status = "moved"
    else:
        status = "unchanged"
    return StaleTrace(
        status=status,
        original_commit=original_commit,
        original_path=path,
        original_line=original_line,
        current_path=current_path,
        current_line=current_line,
        commits=_commits(log),
        original_snippet=orig_snip,
        current_snippet=cur_snip,
        snippet_start_original=orig_start,
        snippet_start_current=cur_start,
    )


def _commits(log: str) -> list[TraceCommit]:
    out = []
    for row in log.splitlines():
        if "\t" in row:
            sha, subject = row.split("\t", 1)
            out.append(TraceCommit(sha=sha, subject=subject))
    return out


# Worktrees used by the Fix step

def worktree_for_branch(repo: Path, branch: str) -> Path | None:
    """The worktree that has `branch` checked out, if any."""
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    current: Path | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == f"branch refs/heads/{branch}" and current:
            return current
    return None


def worktree_detached_at(repo: Path, oid: str, ignore: Path | None = None) -> Path | None:
    """A worktree with a detached HEAD exactly at `oid`, if any. Worktrees under
    `ignore` (reviewr's own scratch worktrees) don't count."""
    out = _git(repo, "worktree", "list", "--porcelain").stdout
    for block in out.split("\n\n"):
        lines = block.splitlines()
        if "detached" in lines and f"HEAD {oid}" in lines:
            path = Path(lines[0].removeprefix("worktree "))
            if ignore and path.resolve().is_relative_to(ignore.resolve()):
                continue
            return path
    return None


def ensure_branch_worktree(repo: Path, path: Path, branch: str, start: str) -> Path:
    """A worktree with the PR's real branch checked out, for when you don't have one.

    Uses your local branch as it is if it exists (never resets it); otherwise creates it
    at `start` and tracks origin/<branch>.
    """
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    if _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        _git(repo, "worktree", "add", str(path), branch)
    else:
        _git(repo, "worktree", "add", "-b", branch, str(path), start)
        _git(repo, "branch", f"--set-upstream-to=origin/{branch}", branch, check=False)
    return path


def create_draft_worktree(repo: Path, path: Path, start: str) -> Path:
    if path.exists():
        remove_worktree(repo, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(path), start)
    return path


def remove_worktree(repo: Path, path: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(path), check=False)


def head_sha(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def working_diff(worktree: Path, exclude: list[str] | tuple[str, ...] = ()) -> str:
    """All uncommitted changes against HEAD, new files included, without staging
    anything: the worktree may be yours, and your index is left as it is. `exclude`
    keeps paths (dependency symlinks) out of the diff."""
    excl = [f":(exclude){p}" for p in exclude]
    tracked = _git(worktree, "diff", "--binary", "HEAD", "--", ".", *excl).stdout
    untracked = _git(worktree, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", *excl).stdout
    parts = [tracked]
    for rel in filter(None, untracked.split("\0")):
        # --no-index exits 1 when files differ, which is the expected case here.
        parts.append(_git(worktree, "diff", "--binary", "--no-index", "--", "/dev/null", rel, check=False).stdout)
    return "".join(parts)


def unstaged_diff(worktree: Path, exclude: list[str] | tuple[str, ...] = ()) -> str:
    """Changes not yet staged (working tree against the index), new files included. Used
    where the index holds a baseline and an agent's edits are what's on top of it."""
    excl = [f":(exclude){p}" for p in exclude]
    parts = [_git(worktree, "diff", "--binary", "--", ".", *excl).stdout]
    untracked = _git(worktree, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", *excl).stdout
    for rel in filter(None, untracked.split("\0")):
        parts.append(_git(worktree, "diff", "--binary", "--no-index", "--", "/dev/null", rel, check=False).stdout)
    return "".join(parts)


_PATCH_FILE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


def patch_paths(patch: str) -> list[str]:
    """Every path a patch touches, old and new names of renames included."""
    out: list[str] = []
    for pair in _PATCH_FILE.findall(patch):
        for p in pair:
            if p not in out:
                out.append(p)
    return out


def dirty_paths(worktree: Path, paths: list[str]) -> list[str]:
    if not paths:
        return []
    out = _git(worktree, "status", "--porcelain", "-z", "--", *paths).stdout
    return [entry[3:] for entry in out.split("\0") if len(entry) > 3]


def set_aside(worktree: Path, paths: list[str]) -> dict[str, bytes | None]:
    """Your working-tree versions of `paths` (None for a file you deleted)."""
    return {p: (worktree / p).read_bytes() if (worktree / p).is_file() else None for p in paths}


def put_back(worktree: Path, saved: dict[str, bytes | None]) -> None:
    """Write files saved by set_aside back exactly as they were (unstaged)."""
    for p, data in saved.items():
        target = worktree / p
        if data is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)


def _blob(worktree: Path, rev: str, path: str) -> bytes | None:
    proc = subprocess.run(["git", "-C", str(worktree), "show", f"{rev}:{path}"], capture_output=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


def merge_back(worktree: Path, saved: dict[str, bytes | None], base_rev: str) -> list[str]:
    """Put your saved edits back on top of what HEAD has now, merging each file three
    ways: the file at `base_rev` (before the commit), at HEAD (after it), and yours.
    Files that conflict keep conflict markers; returns their paths."""
    conflicts: list[str] = []
    for p, mine in saved.items():
        base, theirs = _blob(worktree, base_rev, p), _blob(worktree, "HEAD", p)
        target = worktree / p
        if mine == base or mine == theirs:
            continue  # nothing of yours to bring back
        if theirs == base:  # the commit didn't change this file after all
            put_back(worktree, {p: mine})
            continue
        if mine is None or theirs is None:  # deleted on one side, changed on the other
            conflicts.append(p)
            continue  # keep the committed version; your deletion or edit is the conflict
        with tempfile.TemporaryDirectory(prefix="reviewr-merge-") as tmp:
            files = []
            for name, data in (("yours", mine), ("before", base or b""), ("committed", theirs)):
                f = Path(tmp) / name
                f.write_bytes(data)
                files.append(str(f))
            proc = subprocess.run(
                ["git", "merge-file", "-p", "-L", "your edits", "-L", "before", "-L", "reviewr's commit", *files],
                capture_output=True, check=False,
            )
        if proc.returncode < 0 or (proc.returncode > 0 and not proc.stdout):  # e.g. a binary file
            put_back(worktree, {p: mine})
            conflicts.append(p)
            continue
        target.write_bytes(proc.stdout)
        if proc.returncode > 0:
            conflicts.append(p)
    return conflicts


def apply_and_commit(worktree: Path, patch: str, message: str, commit=None) -> tuple[str, list[str]]:
    """Apply a patch and commit only the files it touches, never mixing your edits into
    the commit. If you have uncommitted edits to those files, they're set aside first
    and merged back on top of the commit afterwards. Returns the commit and the files
    where your edits conflicted with it (left with conflict markers).

    `commit(paths)` makes the commit and returns its sha (by default, a plain git commit
    of `paths`). If applying or committing fails, the files are restored as they were."""
    paths = patch_paths(patch)
    saved = set_aside(worktree, dirty_paths(worktree, paths))
    base = head_sha(worktree)
    restore_paths(worktree, list(saved))
    try:
        apply_index(worktree, patch)
        if commit is not None:
            sha = commit(paths)
        else:
            _git(worktree, "commit", "-m", message, "--", *paths)
            sha = head_sha(worktree)
    except GitError:
        restore_paths(worktree, paths)
        put_back(worktree, saved)
        raise
    return sha, merge_back(worktree, saved, base)


def apply_index(worktree: Path, patch: str) -> None:
    """Apply a patch to the working tree and the index (three-way if needed)."""
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(patch)
    _git(worktree, "apply", "--3way", "--index", f.name)


def staged_as(worktree: Path, patch: str, paths: list[str]) -> bool:
    """Whether `patch` is already applied and staged, with no other edits to its files:
    what a commit that failed after applying it leaves behind."""
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(patch)
    if _git(worktree, "apply", "--check", "-R", "--cached", f.name, check=False).returncode != 0:
        return False
    return _git(worktree, "diff", "--quiet", "--", *paths, check=False).returncode == 0


def restore_paths(worktree: Path, paths: list[str]) -> None:
    """Put `paths` back as they are at HEAD, in the index and the working tree; files
    that don't exist at HEAD are removed."""
    for p in paths:
        if _git(worktree, "cat-file", "-e", f"HEAD:{p}", check=False).returncode == 0:
            _git(worktree, "restore", "--source=HEAD", "--staged", "--worktree", "--", p, check=False)
        else:
            _git(worktree, "rm", "-q", "--cached", "-f", "--ignore-unmatch", "--", p, check=False)
            (worktree / p).unlink(missing_ok=True)


def commit_paths(worktree: Path, message: str, paths: list[str]) -> str:
    """Commit exactly `paths` (new, changed or deleted), leaving everything else in the
    worktree and index as it was."""
    if not paths:
        raise GitError("No files selected to commit.")
    _git(worktree, "add", "-A", "--", *paths)
    _git(worktree, "commit", "-m", message, "--", *paths)
    return head_sha(worktree)


def apply_to_worktree(worktree: Path, patch: str) -> None:
    """Apply a patch to the working tree only (no commit)."""
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(patch)
    _git(worktree, "apply", f.name)


def commits_ahead(worktree: Path, base: str) -> int:
    """Commits on HEAD's own line (first parents) that `base` doesn't have. A merge of
    main counts as one commit, not as every commit it brings in from main."""
    return int(_git(worktree, "rev-list", "--first-parent", "--count", f"{base}..HEAD", check=False).stdout.strip() or 0)


def ahead_behind(worktree: Path, oid: str) -> tuple[int, int]:
    """Commits on HEAD not in `oid`, and in `oid` not on HEAD (e.g. vs the PR head on GitHub)."""
    out = _git(worktree, "rev-list", "--left-right", "--count", f"HEAD...{oid}", check=False).stdout.split()
    return (int(out[0]), int(out[1])) if len(out) == 2 else (0, 0)


def current_branch(worktree: Path) -> str | None:
    out = _git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    return out or None


def push(worktree: Path, remote_branch: str) -> None:
    """Fast-forward push only: fails if someone else pushed to the branch meanwhile."""
    _git(worktree, "push", "origin", f"HEAD:refs/heads/{remote_branch}")
