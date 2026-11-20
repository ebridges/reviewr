import { useState } from "react";
import type { CheckRun, WorkItem } from "../api";
import { useAIName } from "../lib";
import { failedLabel } from "./ProposedFix";

const STATUS: Record<CheckRun["status"], { label: string; cls: string; mark: string }> = {
  passed: { label: "Passed", cls: "ok", mark: "✓" },
  failed: { label: "Failed", cls: "warn", mark: "✗" },
  error: { label: "Couldn't run", cls: "warn", mark: "!" },
  skipped: { label: "Skipped", cls: "", mark: "–" },
};

/** Lint and test results for a work item's changed files. */
export function ChecksPanel({
  item, onRun, running, onFixFailures,
}: { item: WorkItem; onRun: () => void; running: boolean; onFixFailures?: (summary: string, label: string) => void }) {
  const report = item.check_report;
  const busy = running || item.checks_running;
  const ai = useAIName();
  const failures = report?.runs.filter((r) => r.status === "failed" || r.status === "error") ?? [];

  return (
    <section className="card stack-gap">
      <div className="row-between">
        <h2>Checks</h2>
        <button className="btn sm" onClick={onRun} disabled={busy || item.status === "drafting"}>
          {busy ? "Running…" : report ? "Run again" : "Run checks"}
        </button>
      </div>
      <p className="muted small">
        Lint and tests for the files this item changes, only, using the dependencies on this PR's branch. Tests are the
        ones that import or are named after those files. End-to-end suites are never run here.
      </p>
      {busy && <p className="muted small">Running checks on {report?.scope.length ?? "the"} changed files…</p>}
      {!report && !busy && <p className="muted small">Not run yet.</p>}
      {report && (
        <>
          <div className="muted tiny">
            {report.scope.length} changed file{report.scope.length === 1 ? "" : "s"} · {new Date(report.ran_at).toLocaleTimeString()}
          </div>
          {report.runs.map((r) => <CheckRow key={r.name} run={r} />)}
          {report.runs.length === 0 && <p className="muted small">No checks are configured for this repository.</p>}
          {failures.length > 0 && onFixFailures && (
            <button className="btn sm" disabled={busy} onClick={() => onFixFailures(
              failures.map((f) => `${f.name} ${f.status === "error" ? "couldn't run" : "failed"}:\n${f.output.slice(-1500)}`).join("\n\n"),
              failedLabel(failures.map((f) => f.name)),
            )}>
              Ask {ai} to fix {failures.length === 1 ? "it" : "these"}
            </button>
          )}
        </>
      )}
    </section>
  );
}

export function CheckRow({ run }: { run: CheckRun }) {
  const [open, setOpen] = useState(false);
  const s = STATUS[run.status];
  const detail = run.kind === "test"
    ? run.tests.length ? `${run.tests.length} test file${run.tests.length === 1 ? "" : "s"}` : ""
    : run.files.length ? `${run.files.length} file${run.files.length === 1 ? "" : "s"}` : "";
  return (
    <div className="check-run">
      <div className="row-wrap">
        <span className={`pill ${s.cls}`}>{s.mark} {s.label}</span>
        <span className="strong small grow">{run.name}</span>
        {detail && <span className="muted tiny">{detail}{run.duration_s ? ` · ${run.duration_s}s` : ""}</span>}
      </div>
      {run.reason && <div className="muted tiny">{run.reason}</div>}
      {(run.output || run.tests.length > 0) && (
        <button className="link tiny" onClick={() => setOpen(!open)}>{open ? "Hide details" : "Details"}</button>
      )}
      {open && (
        <div className="stack-gap">
          {run.command && <div className="mono tiny muted">{run.command}</div>}
          {run.tests.length > 0 && (
            <details>
              <summary className="tiny">Tests run</summary>
              <ul className="commits">{run.tests.slice(0, 200).map((t) => <li key={t} className="mono">{t}</li>)}</ul>
            </details>
          )}
          {run.output && <pre className="code"><div className="meta">{run.output}</div></pre>}
        </div>
      )}
    </div>
  );
}
