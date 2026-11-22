"""Reviewing and fixing a PR's failing CI checks, following the same steps as review
comments and updates from main: start, triage, fix, finish.

- Status: the failing checks and their annotations come from one GraphQL query.
- Start: each failing job's log is downloaded (REST, D11), cut down to the step that
  failed, and the AI sorts the failures into: fix in this PR, re-run (flaky or
  infrastructure), or not this PR's problem.
- Fix: for a failure to fix, the coding agent works in a throwaway worktree at your
  checkout's HEAD; you see its diff, and Apply commits it to your checkout. Re-runs go
  to GitHub only when you confirm.
- Finish: push the fixes (after you confirm); CI runs again on GitHub.

The session is kept in <root>/ci/session.json.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from reviewr import envs, fixwork, git
from reviewr.config import Config
from reviewr.github import GitHubClient, GitHubError
from reviewr.jobs import Jobs
from reviewr.models import PullRequest
from reviewr.state import ProposedFix

log = logging.getLogger(__name__)

Category = Literal["fix", "rerun", "unrelated"]
Status = Literal["open", "fixed", "rerun", "dismissed"]
OK = {"SUCCESS", "SKIPPED", "NEUTRAL"}
STATUS_TTL_S = 60
EXCERPT_LINES = 160

_lock = threading.RLock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ci")
_jobs = Jobs()  # PRs with an analysis or fix job running in this process
_status_cache: dict[str, tuple[float, dict]] = {}


class CIError(RuntimeError):
    pass


class Annotation(BaseModel):
    path: str
    line: int | None = None
    level: str
    message: str


class CIFailure(BaseModel):
    id: str  # the Actions job id (check run databaseId), or the status context name
    name: str
    workflow: str | None = None
    conclusion: str
    url: str | None = None
    required: bool = False
    job_id: int | None = None  # set for GitHub Actions jobs: logs and re-runs need it
    annotations: list[Annotation] = Field(default_factory=list)
    excerpt: str = ""
    category: Category | None = None
    title: str = ""
    reason: str = ""
    files: list[str] = Field(default_factory=list)
    ai_name: str | None = None
    status: Status = "open"
    fix: ProposedFix | None = None
    commit_sha: str | None = None
    edit_conflicts: list[str] = Field(default_factory=list)  # see WorkItem.edit_conflicts


class CISession(BaseModel):
    head_sha: str  # the commit CI ran on (the PR head on GitHub)
    started_at: datetime
    status: Literal["analyzing", "ready"]
    error: str | None = None
    failures: dict[str, CIFailure] = Field(default_factory=dict)
    order: list[str] = Field(default_factory=list)
    pushed: bool = False


# Paths and persistence

def _dir(cfg: Config, pr: PullRequest) -> Path:
    return fixwork._root(cfg, pr) / "ci"


def _session_path(cfg: Config, pr: PullRequest) -> Path:
    return _dir(cfg, pr) / "session.json"


def load(cfg: Config, pr: PullRequest) -> CISession | None:
    p = _session_path(cfg, pr)
    return CISession.model_validate_json(p.read_text()) if p.exists() else None


def _save(cfg: Config, pr: PullRequest, s: CISession) -> None:
    p = _session_path(cfg, pr)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(s.model_dump_json())
    tmp.replace(p)


def update(cfg: Config, pr: PullRequest, change) -> CISession:
    with _lock:
        s = load(cfg, pr)
        if s is None:
            raise CIError("There's no CI review in progress.")
        change(s)
        _save(cfg, pr, s)
        return s


def load_current(cfg: Config, pr: PullRequest) -> CISession | None:
    """The session, with jobs cut off by a restart marked as such."""
    with _lock:
        s = load(cfg, pr)
        if not s or pr.ref.key in _jobs:
            return s
        changed = False
        if s.status == "analyzing":
            s.status, s.error, changed = "ready", "Analysis was interrupted because reviewr restarted. Try again.", True
        for f in s.failures.values():
            if f.fix and f.fix.status == "running":
                f.fix.status, f.fix.error, changed = "failed", "Interrupted because reviewr restarted. Ask again.", True
        if changed:
            _save(cfg, pr, s)
        return s


def discard(cfg: Config, pr: PullRequest) -> None:
    with _lock:
        p = _session_path(cfg, pr)
        if p.exists():
            p.unlink()


# Status from GitHub

def _failures_from(data: dict) -> tuple[str, str | None, list[CIFailure]]:
    pull = data["repository"]["pullRequest"]
    nodes = pull["commits"]["nodes"]
    commit = nodes[0]["commit"] if nodes else {}
    rollup = commit.get("statusCheckRollup") or {}
    out: list[CIFailure] = []
    for n in (rollup.get("contexts") or {}).get("nodes", []):
        if n["__typename"] == "CheckRun":
            if n.get("status") != "COMPLETED" or n.get("conclusion") in OK or n.get("conclusion") is None:
                continue
            run = (n.get("checkSuite") or {}).get("workflowRun") or {}
            out.append(CIFailure(
                id=str(n["databaseId"]), name=n["name"], workflow=(run.get("workflow") or {}).get("name"),
                conclusion=n["conclusion"], url=n.get("detailsUrl"), required=bool(n.get("isRequired")),
                job_id=n["databaseId"] if run else None,
                annotations=[
                    Annotation(path=a["path"], line=(a.get("location") or {}).get("start", {}).get("line"),
                               level=a["annotationLevel"], message=(a.get("message") or "").strip())
                    for a in n["annotations"]["nodes"] if a["annotationLevel"] in ("FAILURE", "WARNING")
                ],
            ))
        elif n.get("state") in ("FAILURE", "ERROR"):
            out.append(CIFailure(id=n["context"], name=n["context"], conclusion=n["state"], url=n.get("targetUrl"),
                                 annotations=[Annotation(path="", level="FAILURE", message=n.get("description") or "")]))
    return commit.get("oid", pull.get("headRefOid", "")), rollup.get("state"), out


def status(gh: GitHubClient, pr: PullRequest, fresh: bool = False) -> dict:
    """The PR's CI state and failing checks (cached for a minute)."""
    key = pr.ref.key
    if not fresh and (hit := _status_cache.get(key)) and time.time() - hit[0] < STATUS_TTL_S:
        return hit[1]
    data = gh.query("ci_checks", owner=pr.ref.owner, repo=pr.ref.repo, number=pr.ref.number)
    head, state, failures = _failures_from(data)
    result = {"head_sha": head, "state": state, "failures": [f.model_dump(include={"id", "name", "workflow", "conclusion", "url", "required"}) for f in failures]}
    _status_cache[key] = (time.time(), result)
    return result


# Logs

_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;]*m|\[\d+(;\d+)*m")


def excerpt(log_text: str, limit: int = EXCERPT_LINES) -> str:
    """The part of a job log that matters: the step that failed (from its `##[group]Run`
    line to the end), cut to the last `limit` lines, plus every `##[error]` line."""
    lines = [_ANSI.sub("", _STAMP.sub("", line)) for line in log_text.splitlines()]
    first_error = next((i for i, line in enumerate(lines) if line.startswith("##[error]")), None)
    if first_error is None:
        return "\n".join(lines[-limit:])
    step = max((i for i in range(first_error) if lines[i].startswith("##[group]Run ")), default=max(0, first_error - limit))
    section = lines[step:]
    if len(section) > limit:
        errors = [line for line in section[:-limit] if line.startswith("##[error]")]
        section = [section[0], f"… {len(section) - limit} lines left out …", *errors, *section[-limit:]]
    return "\n".join(section)


def _log_excerpt(cfg: Config, pr: PullRequest, gh: GitHubClient, f: CIFailure) -> str:
    if f.job_id is None:
        return ""
    cache = _dir(cfg, pr) / "logs" / f"{f.job_id}.txt"
    if cache.exists():
        return cache.read_text()
    try:
        text = excerpt(gh.rest("GET", f"/repos/{pr.ref.owner}/{pr.ref.repo}/actions/jobs/{f.job_id}/logs").text)
    except (GitHubError, httpx.HTTPError) as e:  # the log may have expired; the annotations remain
        log.warning("Couldn't download the log for job %s: %s", f.job_id, e)
        return f"(The log couldn't be downloaded: {e})"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(text)
    return text


# Start and analysis

def start(cfg: Config, pr: PullRequest, gh: GitHubClient, analyze) -> CISession:
    """Fetch the failing checks and their logs, then triage them in the background."""
    with _lock:
        existing = load(cfg, pr)
        if existing and existing.status == "analyzing" and pr.ref.key in _jobs:
            raise CIError("The failures are already being analyzed.")
        data = gh.query("ci_checks", owner=pr.ref.owner, repo=pr.ref.repo, number=pr.ref.number)
        head, _, failures = _failures_from(data)
        _status_cache.pop(pr.ref.key, None)
        if not failures:
            raise CIError("No checks are failing on this PR.")
        s = CISession(head_sha=head, started_at=datetime.now(UTC), status="analyzing",
                      failures={f.id: f for f in failures}, order=[f.id for f in failures])
        _save(cfg, pr, s)
        _jobs.add(pr.ref.key)
    _executor.submit(_analyze_job, cfg, pr, gh, analyze)
    return s


def reanalyze(cfg: Config, pr: PullRequest, gh: GitHubClient, analyze) -> CISession:
    def mark(s: CISession) -> None:
        s.status, s.error = "analyzing", None

    s = update(cfg, pr, mark)
    _jobs.add(pr.ref.key)
    _executor.submit(_analyze_job, cfg, pr, gh, analyze)
    return s


def _analyze_job(cfg: Config, pr: PullRequest, gh: GitHubClient, analyze) -> None:
    try:
        s = load(cfg, pr)
        excerpts = {fid: f.excerpt or _log_excerpt(cfg, pr, gh, f) for fid, f in s.failures.items()}
        update(cfg, pr, lambda s: [setattr(f, "excerpt", excerpts.get(fid, "")) for fid, f in s.failures.items()])
        s = load(cfg, pr)
        results = analyze(s, [f for f in s.failures.values() if f.status == "open"])

        def apply(s: CISession) -> None:
            for fid, r in results.items():
                if fid in s.failures and s.failures[fid].status == "open":
                    s.failures[fid] = s.failures[fid].model_copy(update=r)
            s.status = "ready"

        update(cfg, pr, apply)
    except Exception as e:  # shown on the page; failures stay untriaged
        log.exception("Analyzing CI failures failed")
        message = str(e) or type(e).__name__

        def failed(s: CISession) -> None:
            s.status, s.error = "ready", f"Couldn't analyze the failures: {message}"

        update(cfg, pr, failed)
    finally:
        _jobs.discard(pr.ref.key)


# Fixing

def _fix_prompt(pr: PullRequest, f: CIFailure) -> str:
    notes = "\n".join(f"- {a.path}{f':{a.line}' if a.line else ''}: {a.message}" for a in f.annotations) or "(none)"
    return f"""A CI check is failing on the pull request "{pr.title}" (branch {pr.head_ref}).
The repository is checked out in the current directory at the PR's branch.

Check: {f.name}{f" (workflow: {f.workflow})" if f.workflow else ""}
{f"Diagnosis so far: {f.reason}" if f.reason else ""}

Annotations GitHub attached to the failure:
{notes}

The failing step's log:
<log>
{f.excerpt[-12000:]}
</log>

Change the code so this check passes, with the smallest change that does it. Keep what
the PR is doing. Edit files only; don't run git commands that change the repository (no
add, commit, checkout, restore, stash or reset). If the fix is to regenerate a file with a
command you can't run, say so and which command. When you're done, say in two or three
sentences what you changed and why."""


def start_fix(cfg: Config, pr: PullRequest, failure_id: str, ai_name: str) -> CISession:
    from reviewr.agent import run_agent  # slow import; only when needed

    s = load(cfg, pr)
    if not s or failure_id not in s.failures:
        raise CIError("Unknown failure.")
    f = s.failures[failure_id]
    if f.fix and f.fix.status in ("running", "ready"):
        raise CIError("A proposed fix is already waiting. Apply or discard it first.")
    work = fixwork.work_worktree(cfg, pr)
    base = git.head_sha(work)

    def mark(s: CISession) -> None:
        s.failures[failure_id].fix = ProposedFix(status="running", failure=f.excerpt[-3000:], label=f.name, ai_name=ai_name)

    s = update(cfg, pr, mark)
    _jobs.add(pr.ref.key)
    _executor.submit(_fix_job, cfg, pr, failure_id, base, _fix_prompt(pr, f), run_agent)
    return s


def _fix_job(cfg: Config, pr: PullRequest, failure_id: str, base: str, prompt: str, run_agent) -> None:
    repo_cfg = cfg.repos[pr.ref.slug]
    scratch = _dir(cfg, pr) / f"fix-{re.sub(r'[^A-Za-z0-9]+', '-', failure_id)}"
    try:
        git.create_draft_worktree(repo_cfg.path, scratch, base)
        output = run_agent(scratch, prompt, cfg.llm.model, pr.ref.slug)
        diff = git.unstaged_diff(scratch, envs.provided_paths(repo_cfg))

        def done(s: CISession) -> None:
            if fix := s.failures[failure_id].fix:
                fix.status, fix.diff, fix.summary, fix.error = "ready", diff, output.strip()[-2000:], None

        update(cfg, pr, done)
    except Exception as e:
        log.exception("Proposing a CI fix failed")
        message = str(e) or type(e).__name__

        def failed(s: CISession) -> None:
            if fix := s.failures[failure_id].fix:
                fix.status, fix.error = "failed", message

        update(cfg, pr, failed)
    finally:
        git.remove_worktree(repo_cfg.path, scratch)
        _jobs.discard(pr.ref.key)


def apply_fix(cfg: Config, pr: PullRequest, failure_id: str, allow_conflicts: bool = False) -> CISession:
    """Commit the proposed fix to your checkout (only its files; your uncommitted edits
    to them are merged back on top). Raises git.EditsWouldConflict if they would
    conflict, unless `allow_conflicts`."""
    with _lock:
        s = load(cfg, pr)
        f = s.failures.get(failure_id) if s else None
        if not f or not f.fix or f.fix.status != "ready" or not f.fix.diff.strip():
            raise CIError("There's no proposed fix to apply.")
        try:
            sha, _, conflicts = fixwork.apply_patch_and_commit(
                cfg, pr, fixwork.work_worktree(cfg, pr), f.fix.diff, f"Fix CI: {f.title or f.name}", allow_conflicts)
        except git.EditsWouldConflict:
            raise  # the page asks what to do
        except git.GitError as e:
            raise CIError(str(e)) from e
        f.fix, f.status, f.commit_sha, f.edit_conflicts = None, "fixed", sha, conflicts
        _save(cfg, pr, s)
        return s


def discard_fix(cfg: Config, pr: PullRequest, failure_id: str) -> CISession:
    def clear(s: CISession) -> None:
        f = s.failures[failure_id]
        if f.fix and f.fix.status == "running":
            raise CIError("The agent is still working on it.")
        f.fix = None

    return update(cfg, pr, clear)


def rerun(cfg: Config, pr: PullRequest, gh: GitHubClient, failure_id: str) -> CISession:
    """Re-run one failed Actions job on GitHub."""
    s = load(cfg, pr)
    f = s.failures.get(failure_id) if s else None
    if not f or f.job_id is None:
        raise CIError("Only GitHub Actions jobs can be re-run from here.")
    gh.rest("POST", f"/repos/{pr.ref.owner}/{pr.ref.repo}/actions/jobs/{f.job_id}/rerun")
    _status_cache.pop(pr.ref.key, None)
    return update(cfg, pr, lambda s: setattr(s.failures[failure_id], "status", "rerun"))


def push(cfg: Config, pr: PullRequest) -> CISession:
    try:
        git.push(fixwork.work_worktree(cfg, pr), pr.head_ref)
    except git.GitError as e:
        if re.search(r"timed out|Could not resolve|Could not read from remote|Connection refused", str(e)):
            raise CIError(f"Couldn't reach GitHub, so nothing was pushed. Try again when the connection works.\n\n{e}") from e
        raise CIError(str(e)) from e
    _status_cache.pop(pr.ref.key, None)
    return update(cfg, pr, lambda s: setattr(s, "pushed", True))
