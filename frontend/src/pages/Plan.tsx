import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { PRContext } from "../App";
import { Spinner } from "../components/Spinner";
import { BrandIcon } from "../components/Brand";
import { api, type Decision, type WorkItem } from "../api";
import { decisionLabel, prHref, threadTitle, useAIName, where } from "../lib";

export const STATUS_LABEL: Record<WorkItem["status"], string> = {
  pending: "Not started",
  drafting: "Drafting",
  ready: "Patch ready",
  failed: "Draft failed",
  committed: "Committed",
};

export function itemStatus(w: WorkItem) {
  if (w.mode === "me" && w.status !== "committed") return "Yours";
  return STATUS_LABEL[w.status];
}

// What happens to threads with each decision, in the order the summary lists them.
const OUTCOME: { key: Decision; text: string }[] = [
  { key: "fix", text: "Grouped into work items below; each becomes one commit, and the reply links it." },
  { key: "already_done", text: "Reply points at the change that addressed it; the thread is resolved." },
  { key: "reply", text: "Answered in a reply, written in Respond." },
  { key: "push_back", text: "A reply explains why the code stays; written in Respond." },
  { key: "defer", text: "A reply says it's handled separately; file it in Jira below." },
  { key: "no_action", text: "Nothing is posted; the thread is left as it is." },
];

export function Plan({ ctx }: { ctx: PRContext }) {
  const { pr, state } = ctx.data!;
  const open = pr.threads.filter((t) => !t.is_resolved);
  const undecided = open.filter((t) => !state.decisions[t.id]).length;
  const counts = new Map<Decision, number>();
  for (const t of open) {
    const d = state.decisions[t.id];
    if (d) counts.set(d.decision, (counts.get(d.decision) ?? 0) + 1);
  }
  const fixes = counts.get("fix") ?? 0;
  const toPost = [...counts.entries()].filter(([k]) => k !== "no_action").reduce((a, [, n]) => a + n, 0);

  return (
    <main className="page narrow">
      <div className="page-head">
        <div className="grow">
          <h1>{undecided ? `${undecided} thread${undecided === 1 ? "" : "s"} still undecided` : fixes ? `${fixes} thread${fixes === 1 ? "" : "s"} to fix` : "Nothing to fix"}</h1>
          <p className="muted">What triage decided, and what happens next for each group.</p>
        </div>
      </div>

      {undecided > 0 && (
        <div className="banner">
          {undecided} open thread{undecided === 1 ? " has" : "s have"} no decision yet. <a href={prHref(pr.ref, "triage")}>Finish triage</a>, or plan with what's decided.
        </div>
      )}

      <section className="card">
        <h2>What triage decided</h2>
        {counts.size === 0 && <p className="muted small">No decisions yet.</p>}
        {OUTCOME.filter((o) => counts.get(o.key)).map((o) => (
          <div key={o.key} className="bucket">
            <div className="bucket-n sm">{counts.get(o.key)}</div>
            <div className="grow">
              <div className="strong small">{decisionLabel(o.key)}</div>
              <div className="muted small">{o.text}</div>
              {o.key === "defer" && <JiraSection ctx={ctx} />}
            </div>
            {o.key === "fix" && <RegroupButton ctx={ctx} />}
          </div>
        ))}
      </section>

      {fixes > 0 ? (
        <WorkItems ctx={ctx} fixes={fixes} />
      ) : (
        !undecided && counts.size > 0 && (
          <section className="card empty-next">
            {toPost > 0 ? (
              <>
                <p><strong>Nothing to fix.</strong> The rest happens in Respond, where you review the replies and post them.</p>
              </>
            ) : (
              <>
                <p><strong>Nothing to fix or post on this PR.</strong> Every thread is No action, so there's nothing left to do here.</p>
              </>
            )}
          </section>
        )
      )}
    </main>
  );
}

/** Regrouping replaces the work items, so it's offered only before any of them is started. */
function RegroupButton({ ctx }: { ctx: PRContext }) {
  const { pr, state } = ctx.data!;
  const plan = useMutation({ mutationFn: () => api.plan(pr.ref), onSuccess: ctx.setState });
  if (!state.plan.length || state.plan.some((w) => w.status !== "pending")) return null;
  return (
    <div className="stack-tight">
      <button className="btn sm" onClick={() => plan.mutate()} disabled={plan.isPending} title="Ask again how to group the fixes into work items">
        {plan.isPending ? "Grouping…" : "Regroup"}
      </button>
      {plan.error && <span className="error tiny">{(plan.error as Error).message}</span>}
    </div>
  );
}

function WorkItems({ ctx, fixes }: { ctx: PRContext; fixes: number }) {
  const { pr, state } = ctx.data!;
  const aiName = useAIName();
  const [expanded, setExpanded] = useState<number>();
  const plan = useMutation({ mutationFn: () => api.plan(pr.ref), onSuccess: ctx.setState });
  const draftAll = useMutation({ mutationFn: () => api.draftAll(pr.ref), onSuccess: ctx.setState });
  const setMode = (id: number, mode: "agent" | "me") => api.patchItem(pr.ref, id, { mode }).then(ctx.setState);

  const counts = { drafting: 0, ready: 0, pending: 0, failed: 0, committed: 0 };
  for (const w of state.plan) if (w.mode === "agent" || w.status === "committed") counts[w.status]++;
  const agentWaiting = state.plan.filter((w) => w.mode === "agent" && (w.status === "pending" || w.status === "failed")).length;

  return (
    <section className="stack">
      <div className="row-between">
        <h2 className="section-title">Work items{state.plan.length ? ` · ${state.plan.length}` : ""}</h2>
        <div className="row-wrap">
          {!state.plan.length && (
            <button className="btn primary" onClick={() => plan.mutate()} disabled={plan.isPending}>
              {plan.isPending ? "Grouping…" : `Group ${fixes} fix${fixes === 1 ? "" : "es"} into work items`}
            </button>
          )}
        </div>
      </div>
      {!state.plan.length && !plan.isPending && (
        <p className="muted small">{aiName} groups the fixes so that each work item is one coherent commit. Agent items are drafted in their own worktree; nothing is pushed until Respond.</p>
      )}
      {plan.error && <div className="banner error">{(plan.error as Error).message}</div>}
      {state.plan.length > 0 && (
        <div className="info-bar">
          <span className="grow small">
            {counts.drafting ? `Agent is drafting ${counts.drafting} item${counts.drafting === 1 ? "" : "s"}. ` : ""}
            {counts.ready} patch{counts.ready === 1 ? "" : "es"} ready · {counts.committed} committed
            {counts.failed ? ` · ${counts.failed} failed` : ""}
          </span>
          {agentWaiting > 0 && (
            <button className="btn sm" onClick={() => draftAll.mutate()} disabled={draftAll.isPending}>
              Draft {agentWaiting} agent item{agentWaiting === 1 ? "" : "s"} in the background
            </button>
          )}
        </div>
      )}
      {draftAll.error && <div className="banner error">{(draftAll.error as Error).message}</div>}
      {state.plan.map((w) => {
        const files = new Set(w.thread_ids.map((id) => pr.threads.find((t) => t.id === id)?.path));
        return (
          <div key={w.id} className="card item">
            <div className="item-row">
              <span className="item-n">{w.id}</span>
              <div className="grow min0">
                <a className="strong" href={prHref(pr.ref, "fix", String(w.id))}>{w.title}</a>
                <div className="muted small">
                  {files.size} file{files.size === 1 ? "" : "s"} ·{" "}
                  <button className="threads-link" aria-expanded={expanded === w.id} onClick={() => setExpanded(expanded === w.id ? undefined : w.id)}>
                    {w.thread_ids.length} thread{w.thread_ids.length === 1 ? "" : "s"} {expanded === w.id ? "▴" : "▾"}
                  </button>
                </div>
              </div>
              <span className={`pill ${w.status === "committed" ? "ok" : w.status === "failed" ? "warn" : ""}`}>
                {(w.status === "drafting" || w.checks_running || w.proposed_fix?.status === "running") && <><Spinner label="Agent working" />&nbsp;</>}
                {itemStatus(w)}
              </span>
              <div className="seg" role="group" aria-label="Who does this item">
                <button className={w.mode === "agent" ? "on" : ""} disabled={w.status === "committed"} onClick={() => setMode(w.id, "agent")}>Agent drafts</button>
                <button className={w.mode === "me" ? "on" : ""} disabled={w.status === "committed"} onClick={() => setMode(w.id, "me")}>I'll do it</button>
              </div>
            </div>
            {expanded === w.id && (
              <ul className="thread-list">
                {w.thread_ids.map((id) => {
                  const t = pr.threads.find((x) => x.id === id);
                  return t && <li key={id}><span className="small">{threadTitle(t, state)}</span> <span className="mono muted tiny">{where(t)}</span></li>;
                })}
              </ul>
            )}
          </div>
        );
      })}
    </section>
  );
}

/** Deferred threads go into one Jira issue; their replies then name it. */
function JiraSection({ ctx }: { ctx: PRContext }) {
  const { pr, state } = ctx.data!;
  const config = useQuery({ queryKey: ["config"], queryFn: api.config });
  const tracked = new Set(state.jira_issues.flatMap((i) => i.thread_ids));
  const deferred = Object.entries(state.decisions).filter(([id, d]) => d.decision === "defer" && !tracked.has(id)).map(([id]) => id);
  const create = useMutation({
    mutationFn: async () => {
      const st = await api.createJira(pr.ref, deferred);
      ctx.setState(st);
      return api.draftReplies(pr.ref, deferred); // replies now name the issue
    },
    onSuccess: ctx.setState,
  });
  const project = config.data?.jira_project;

  return (
    <div className="stack-gap jira" style={{ marginTop: 8 }}>
      {state.jira_issues.map((i) => (
        <div key={i.key} className="small">
          <a href={i.url} target="_blank" rel="noreferrer" className="mono with-icon"><BrandIcon name="atlassian" />{i.key}</a>{" "}
          <span className="muted">· {i.thread_ids.length} deferred thread{i.thread_ids.length === 1 ? "" : "s"}</span>
        </div>
      ))}
      {deferred.length > 0 && (project ? (
        <button
          className="btn sm with-icon"
          disabled={create.isPending}
          onClick={() => {
            if (confirm(`Create a ${project} issue in Jira listing ${deferred.length} deferred thread${deferred.length === 1 ? "" : "s"}?`)) create.mutate();
          }}
        >
          <BrandIcon name="atlassian" />
          {create.isPending ? "Creating…" : `Create a Jira issue for ${deferred.length} deferred`}
        </button>
      ) : (
        <p className="muted small">To file deferred threads in Jira, <a href="#/settings">set up Jira in Settings</a>.</p>
      ))}
      {create.error && <p className="error small">{(create.error as Error).message}</p>}
    </div>
  );
}
