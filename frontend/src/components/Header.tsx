import { Fragment } from "react";
import type { PRView } from "../api";
import { prHref, type Route } from "../lib";
import { MergeBadge } from "../pages/Merge";
import { AISwitch } from "./AISwitch";
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
        <a className="pr-title small" href={pr.url} target="_blank" rel="noreferrer" title={pr.title}>
          #{pr.ref.number} · {pr.title}
        </a>
      )}
      {pr && <WorkspaceMenu prRef={pr.ref} headRef={pr.head_ref} />}
      {pr && <MergeBadge prRef={pr.ref} />}
      <span className="spacer" />
      <AISwitch />
      {route.ref && (
        <nav aria-label="Workflow steps" className="steps">
          {STEPS.map((s, i) => (
            <Fragment key={s.page}>
              {i > 0 && <span className="step-sep" aria-hidden="true">→</span>}
              <a href={prHref(route.ref!, s.page)} className={`step ${route.page === s.page ? "on" : ""}`}
                aria-current={route.page === s.page ? "step" : undefined}>
                {s.label}{counts[s.page] ?? ""}
              </a>
            </Fragment>
          ))}
        </nav>
      )}
      {!route.ref && (
        <a href="#/settings" className="icon-link" aria-label="Settings" title="Settings">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="3" />
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68 1.65 1.65 0 0 0 10 3.17V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
          </svg>
        </a>
      )}
    </header>
  );
}
