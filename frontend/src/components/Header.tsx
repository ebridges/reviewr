import { Fragment } from "react";
import type { PRView } from "../api";
import { prHref, type Route } from "../lib";
import { ciSteps, useCIStatus } from "../pages/CI";
import { mergeStatus, mergeSteps, useBranchStatus, type WorkflowStep } from "../pages/Merge";
import { AISwitch } from "./AISwitch";
import { BrandIcon } from "./Brand";
import { WorkingBar } from "./WorkingBar";
import { RunButton } from "../pages/Run";
import { WorkspaceMenu } from "./WorkspacePanel";

export const STEPS = [
  { page: "overview", label: "Overview" },
  { page: "triage", label: "Triage" },
  { page: "plan", label: "Plan" },
  { page: "fix", label: "Fix" },
  { page: "respond", label: "Respond" },
] as const;

export function Header({ route, view }: { route: Route; view?: PRView }) {
  const pr = view?.pr;
  const st = view?.state;
  const open = pr?.threads.filter((t) => !t.is_resolved) ?? [];
  const decided = open.filter((t) => st?.decisions[t.id]).length;
  const committed = st?.plan.filter((w) => w.status === "committed").length ?? 0;
  const counts: Record<string, string> = {
    triage: pr ? ` · ${decided}/${open.length}` : "",
    fix: st?.plan.length ? ` · ${committed}/${st.plan.length}` : "",
  };

  return (
    <header className="topbar">
      <a className="logo" href="#/"><img src="/icon.svg" alt="" />reviewr</a>
      {route.page !== "prs" && <a href="#/" className="small">← All pull requests</a>}
      {pr && (
        <a className="pr-title small with-icon" href={pr.url} target="_blank" rel="noreferrer" title={`${pr.title} (on GitHub)`}>
          <BrandIcon name="github" />
          #{pr.ref.number} · {pr.title}
        </a>
      )}
      {pr && <WorkspaceMenu prRef={pr.ref} headRef={pr.head_ref} />}
      {pr && <RunButton prRef={pr.ref} on={route.page === "run"} />}
      <span className="spacer" />
      <AISwitch />
      {route.ref && <WorkflowTracker route={route} counts={counts} base={pr?.base_ref ?? "main"} />}
      {!route.ref && (
        <a href="#/settings" className="icon-link" aria-label="Settings" title="Settings">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68 1.65 1.65 0 0 0 10 3.17V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </a>
      )}
      {view && <WorkingBar view={view} />}
    </header>
  );
}

/** One tracker for whichever workflow you're in: the switch picks review comments or
 *  updating from main, and the steps are that workflow's. */
function WorkflowTracker({ route, counts, base }: { route: Route; counts: Record<string, string>; base: string }) {
  const ref = route.ref!;
  const inMerge = route.page === "merge";
  const inCI = route.page === "ci";
  const { session, preview } = useBranchStatus(ref);
  const status = mergeStatus(session, preview);
  const ci = useCIStatus(ref);
  const steps: WorkflowStep[] = inMerge
    ? mergeSteps(ref, session, route.arg)
    : inCI
      ? ciSteps(ref, ci.session, route.arg)
      : STEPS.map((s) => ({ key: s.page, label: s.label + (counts[s.page] ?? ""), href: prHref(ref, s.page), on: route.page === s.page }));
  return (
    <div className="workflow">
      <div className="seg workflow-switch" role="group" aria-label="Workflow">
        <a href={prHref(ref, "overview")} className={inMerge || inCI ? "" : "on"} aria-current={inMerge || inCI ? undefined : "true"}>Review comments</a>
        <a href={prHref(ref, "merge")} className={inMerge ? "on" : ""} aria-current={inMerge ? "true" : undefined}>
          Update from {base}
          {status && <span className={`wf-chip ${status.warn ? "warn" : ""}`} title={status.title}>{status.text}</span>}
        </a>
        {(ci.show || inCI) && (
          <a href={prHref(ref, "ci")} className={inCI ? "on" : ""} aria-current={inCI ? "true" : undefined}>
            Fix CI
            {ci.chip && <span className={`wf-chip ${ci.warn ? "warn" : ""}`}>{ci.chip}</span>}
          </a>
        )}
      </div>
      <nav aria-label="Workflow steps" className="steps">
        {steps.map((s, i) => (
          <Fragment key={s.key}>
            {i > 0 && <span className="step-sep" aria-hidden="true">→</span>}
            {s.href ? (
              <a href={s.href} className={`step ${s.on ? "on" : ""}`} aria-current={s.on ? "step" : undefined}>{s.label}</a>
            ) : (
              <span className="step off">{s.label}</span>
            )}
          </Fragment>
        ))}
      </nav>
    </div>
  );
}
