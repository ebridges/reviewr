"""Fix CI: failing checks from GitHub, log excerpts, and fixes committed to your checkout."""

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from reviewr import ci
from tests.test_merge import setup, sh  # noqa: F401  (setup is a fixture)

LOG = """2026-11-20T14:05:00.0000000Z ##[group]Run npm ci
2026-11-20T14:05:01.0000000Z added 900 packages
2026-11-20T14:05:02.0000000Z Error: boom (from a test that passed)
2026-11-20T14:10:00.0000000Z ##[group]Run npm run build --if-present
2026-11-20T14:10:01.0000000Z \x1b[36;1mnpm run build\x1b[0m
2026-11-20T14:10:31.0000000Z ##[error]src/a.tsx(668,41): error TS2552: Cannot find name 'oldName'.
2026-11-20T14:10:31.6000000Z ##[error]Process completed with exit code 2.
"""


def test_excerpt_starts_at_the_failing_step():
    text = ci.excerpt(LOG)
    assert text.startswith("##[group]Run npm run build")
    assert "TS2552" in text and "boom" not in text and "2026-11-20T" not in text and "\x1b" not in text


def test_excerpt_keeps_errors_when_cut():
    lines = ["2026-11-20T14:00:00Z ##[group]Run pytest", "2026-11-20T14:00:01Z ##[error]early failure"]
    lines += [f"2026-11-20T14:00:02Z line {i}" for i in range(500)]
    text = ci.excerpt("\n".join(lines), limit=50)
    assert "early failure" in text and "line 499" in text and "line 10\n" not in text and "lines left out" in text


def _data(nodes):
    return {"repository": {"pullRequest": {"headRefOid": "abc", "commits": {"nodes": [
        {"commit": {"oid": "abc", "statusCheckRollup": {"state": "FAILURE", "contexts": {"nodes": nodes}}}}]}}}}


def test_failures_from_graphql():
    run = {"workflowRun": {"databaseId": 9, "runNumber": 1, "workflow": {"name": "CI"}}}
    nodes = [
        {"__typename": "CheckRun", "databaseId": 1, "name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS",
         "checkSuite": run, "annotations": {"nodes": []}},
        {"__typename": "CheckRun", "databaseId": 2, "name": "build", "status": "COMPLETED", "conclusion": "FAILURE",
         "detailsUrl": "u", "isRequired": True, "checkSuite": run, "annotations": {"nodes": [
             {"path": "src/a.tsx", "annotationLevel": "FAILURE", "message": "Cannot find name", "location": {"start": {"line": 668}}},
             {"path": ".github", "annotationLevel": "NOTICE", "message": "noise", "location": {"start": {"line": 1}}}]}},
        {"__typename": "CheckRun", "databaseId": 3, "name": "e2e", "status": "IN_PROGRESS", "conclusion": None,
         "checkSuite": run, "annotations": {"nodes": []}},
        {"__typename": "StatusContext", "context": "vercel", "state": "ERROR", "targetUrl": "v", "description": "Build failed"},
    ]
    head, state, failures = ci._failures_from(_data(nodes))
    assert (head, state) == ("abc", "FAILURE")
    assert [f.name for f in failures] == ["build", "vercel"]
    build = failures[0]
    assert build.job_id == 2 and build.required and build.workflow == "CI"
    assert [(a.path, a.line) for a in build.annotations] == [("src/a.tsx", 668)]


def test_fix_is_proposed_then_committed(setup, monkeypatch):  # noqa: F811
    cfg, pr, clone = setup
    head = sh(clone, "rev-parse", "HEAD").strip()
    failure = ci.CIFailure(id="2", name="build", conclusion="FAILURE", job_id=2, excerpt="error: bad quotes in app.py",
                           category="fix", title="Quotes in app.py")
    ci._save(cfg, pr, ci.CISession(head_sha=head, started_at=datetime.now(UTC), status="ready",
                                   failures={"2": failure}, order=["2"]))
    import reviewr.agent

    def agent(worktree, prompt, model, slug=None):
        assert "bad quotes" in prompt and "build" in prompt
        p = Path(worktree) / "app.py"
        p.write_text(p.read_text().replace("'", '"'))
        return "Switched to double quotes."

    monkeypatch.setattr(reviewr.agent, "run_agent", agent)
    ci.start_fix(cfg, pr, "2", "Claude")
    for _ in range(200):
        f = ci.load(cfg, pr).failures["2"]
        if f.fix.status != "running":
            break
        time.sleep(0.05)
    assert f.fix.status == "ready" and "app.py" in f.fix.diff
    assert "'" in (clone / "app.py").read_text()  # nothing in your checkout yet
    s = ci.apply_fix(cfg, pr, "2")
    f = s.failures["2"]
    assert f.status == "fixed" and f.commit_sha == sh(clone, "rev-parse", "HEAD").strip()
    assert sh(clone, "log", "-1", "--format=%s").strip() == "Fix CI: Quotes in app.py"
    assert "'" not in (clone / "app.py").read_text()


def test_interrupted_analysis_is_recoverable(setup):  # noqa: F811
    cfg, pr, _clone = setup
    ci._save(cfg, pr, ci.CISession(head_sha="x", started_at=datetime.now(UTC), status="analyzing"))
    s = ci.load_current(cfg, pr)
    assert s.status == "ready" and "restarted" in s.error
    with pytest.raises(ci.CIError):
        ci.apply_fix(cfg, pr, "nope")


def _session_with_fix(cfg, pr, clone, diff):
    from reviewr.state import ProposedFix

    head = sh(clone, "rev-parse", "HEAD").strip()
    f = ci.CIFailure(id="2", name="build", conclusion="FAILURE", job_id=2, title="Quotes",
                     fix=ProposedFix(status="ready", failure="x", label="build", ai_name="Claude", diff=diff))
    ci._save(cfg, pr, ci.CISession(head_sha=head, started_at=datetime.now(UTC), status="ready", failures={"2": f}, order=["2"]))


def _quote_patch(clone):
    original = (clone / "app.py").read_text()
    (clone / "app.py").write_text(original.replace("'", '"'))
    diff = sh(clone, "diff")
    (clone / "app.py").write_text(original)
    # The file is back to its committed content but has a new mtime. Refresh the index so
    # git sees it as unchanged; otherwise `git apply --index` can refuse it as modified,
    # depending on timestamp resolution (it failed this way on the Linux CI runner).
    sh(clone, "status", "--porcelain")
    return diff


def test_failed_hooks_leave_the_checkout_as_it_was(setup):  # noqa: F811
    from tests.test_merge import _hook

    cfg, pr, clone = setup
    _session_with_fix(cfg, pr, clone, _quote_patch(clone))
    head = sh(clone, "rev-parse", "HEAD")
    _hook(clone, "echo 'eslint: 2 errors' >&2\nexit 1\n")
    with pytest.raises(ci.CIError, match="(?s)commit hooks failed.*eslint: 2 errors"):
        ci.apply_fix(cfg, pr, "2")
    assert sh(clone, "rev-parse", "HEAD") == head
    assert sh(clone, "status", "--porcelain") == ""  # nothing left staged or changed
    assert ci.load(cfg, pr).failures["2"].fix.status == "ready"  # can try again


def test_patch_left_staged_by_a_failed_try_is_committed(setup):  # noqa: F811
    cfg, pr, clone = setup
    diff = _quote_patch(clone)
    _session_with_fix(cfg, pr, clone, diff)
    p = clone.parent / "fix.patch"
    p.write_text(diff)
    sh(clone, "apply", "--index", str(p))  # what the old code left behind
    s = ci.apply_fix(cfg, pr, "2")
    assert s.failures["2"].status == "fixed"
    assert sh(clone, "status", "--porcelain") == "" and "'" not in (clone / "app.py").read_text()


def test_jobs_are_counted_per_pr():
    from reviewr.jobs import Jobs

    j = Jobs()
    j.add("pr")
    j.add("pr")
    j.discard("pr")
    assert "pr" in j  # the second job is still running
    j.discard("pr")
    assert "pr" not in j
