import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import type { PRContext } from "../App";
import { api, type Thread, type WorkItem } from "../api";
import { ChecksPanel } from "../components/ChecksPanel";
import { ProposedFix } from "../components/ProposedFix";
import { DiffView } from "../components/Code";
import { Markdown } from "../components/Markdown";
import { prHref, storageGet, threadTitle, where } from "../lib";
import { itemStatus } from "./Plan";
import { EditConflicts } from "../components/EditConflicts";

export function Fix({ ctx, arg }: { ctx: PRContext; arg?: string }) {
  const { pr, state } = ctx.data!;
  if (!state.plan.length) {
    return (
      <main className="page">
        <p className="muted">No work items yet. <a href={prHref(pr.ref, "plan")}>Group the fixes in Plan</a> first.</p>
      </main>
    );
  }
  const item = state.plan.find((w) => String(w.id) === arg) ?? state.plan.find((w) => w.status !== "committed") ?? state.plan[0];
  const i = state.plan.findIndex((w) => w.id === item.id);
  const prev = state.plan[i - 1];
  const next = state.plan[i + 1];
  const href = (w: WorkItem) => prHref(pr.ref, "fix", String(w.id));

  return (
    <main className="page">
      <nav aria-label="Work items" className="item-nav">
        {prev ? <a className="btn sm" href={href(prev)} title={`Previous: ${prev.title}`} aria-label="Previous item">←</a>
          : <span className="btn sm disabled" aria-hidden="true">←</span>}
        <div className="strip">
          {state.plan.map((w) => (
            <a key={w.id} href={href(w)} title={w.title} className={`strip-item ${w.id === item.id ? "on" : ""}`}
              aria-current={w.id === item.id ? "page" : undefined}>
              <strong>{w.id}</strong> <span className="muted">{itemStatus(w).toLowerCase()}</span>
            </a>
          ))}
        </div>
        {next ? <a className="btn sm" href={href(next)} title={`Next: ${next.title}`} aria-label="Next item">→</a>
          : <span className="btn sm disabled" aria-hidden="true">→</span>}
      </nav>
      <div className="page-head">
        <div className="grow">
          <div className="muted small">Work item {item.id} of {state.plan.length}{item.mode === "me" ? " · you're doing this one" : ""}</div>
          <h1>{item.title}</h1>
        </div>
      </div>
      {item.mode === "agent" ? <AgentItem key={item.id} ctx={ctx} item={item} /> : <OwnItem key={item.id} ctx={ctx} item={item} />}
    </main>
  );
}

function ThreadChecks({ ctx, item, subtitle }: { ctx: PRContext; item: WorkItem; subtitle: string }) {
  const { pr, state } = ctx.data!;
  const done = item.thread_ids.filter((id) => item.checks[id]?.addressed).length;
  // Files open in reviewr's worktree for this PR, which is where your edits and the
  // accepted patches live. An agent's draft isn't there until you accept it.
  const openable = item.mode === "me" || item.status === "committed";
  const editor = storageGet("reviewr:editor") || "vscode";
  const [opened, setOpened] = useState<string>();
  const open = async (path: string, line: number | null) => {
    try {
      const res = await api.open(pr.ref, { path, line, editor });
      setOpened(`Opened: ${res.argv.join(" ")}`);
    } catch (e) {
      setOpened((e as Error).message);
    }
  };
  return (
    <aside className="card checks">
      <h2>Threads · {Object.keys(item.checks).length ? `${done} of ${item.thread_ids.length} addressed` : item.thread_ids.length}</h2>
      <p className="muted small">{subtitle}</p>
      {item.thread_ids.map((id) => {
        const t = pr.threads.find((x) => x.id === id);
        const c = item.checks[id];
        if (!t) return null;
        return (
          <div key={id} className="check-item">
            <span className={c ? (c.addressed ? "ok-text" : "warn-text") : "muted"} aria-label={c ? (c.addressed ? "Addressed" : "Not addressed") : "Not checked"}>
              {c ? (c.addressed ? "✓" : "!") : "○"}
            </span>
            <div className="min0">
              <div className="small">{threadTitle(t, state)}</div>
              {openable ? (
                <button className="link mono tiny" title={`Open in ${editor}, in reviewr's worktree`} onClick={() => open(t.path, t.line ?? t.original_line)}>
                  {where(t)}
                </button>
              ) : (
                <div className="mono muted tiny">{where(t)}</div>
              )}
              {c && <div className="muted tiny">{c.note}</div>}
              {state.decisions[id]?.note && <div className="muted tiny">Your note: {state.decisions[id].note}</div>}
            </div>
          </div>
        );
      })}
      {opened && <p className="muted tiny">{opened}</p>}
    </aside>
  );
}

function defaultMessage(item: WorkItem, reviewers: string[]) {
  const who = reviewers.length ? ` from ${reviewers.join(", ")}` : "";
  return `${item.title}\n\nAddresses ${item.thread_ids.length} review comment${item.thread_ids.length === 1 ? "" : "s"}${who}.`;
}

function useReviewers(ctx: PRContext, item: WorkItem) {
  const { pr } = ctx.data!;
  return [...new Set(item.thread_ids.map((id) => pr.threads.find((t) => t.id === id)?.comments[0]?.author).filter(Boolean) as string[])];
}

function AgentItem({ ctx, item }: { ctx: PRContext; item: WorkItem }) {
  const { pr } = ctx.data!;
  const [message, setMessage] = useState(defaultMessage(item, useReviewers(ctx, item)));
  // The agent's prompt: generated from the threads unless you edited it. `text` holds
  // your unsaved edits; it's saved when you draft.
  const promptQ = useQuery({
    queryKey: ["prompt", pr.ref.number, item.id, item.prompt ?? "", item.instruction, item.thread_ids.join(",")],
    queryFn: () => api.itemPrompt(pr.ref, item.id),
  });
  const [text, setText] = useState<string | null>(null);
  const prompt = text ?? promptQ.data?.prompt ?? "";
  const draft = useMutation({
    mutationFn: async (override?: string) => {
      const t = override ?? prompt;
      const d = promptQ.data;
      if (d && t.trim() === d.default.trim()) {
        if (d.edited) await api.patchItem(pr.ref, item.id, { reset_prompt: true });
      } else if (d && t !== d.prompt) {
        await api.patchItem(pr.ref, item.id, { prompt: t });
      }
      return api.draft(pr.ref, item.id);
    },
    onSuccess: (st) => { ctx.setState(st); setText(null); },
  });
  const accept = useMutation({ mutationFn: () => api.accept(pr.ref, item.id, message), onSuccess: ctx.setState });
  const checks = useMutation({ mutationFn: () => api.runChecks(pr.ref, item.id), onSuccess: ctx.setState });
  const files = (item.patch.match(/^diff --git/gm) ?? []).length;
  const failing = item.check_report?.runs.filter((r) => r.status === "failed" || r.status === "error").length ?? 0;
  const fixFailures = (summary: string) => {
    const next = `${prompt.trimEnd()}\n\nThe previous draft failed these checks. Fix them without changing what it does for the review comments:\n\n${summary}\n`;
    setText(next);
    draft.mutate(next);
  };
  const promptProps = {
    text: prompt, setText, defaultText: promptQ.data?.default, loading: !promptQ.data, busy: draft.isPending,
    onDraft: () => draft.mutate(undefined),
  };

  return (
    <div className="fix-layout">
      <ThreadChecks ctx={ctx} item={item} subtitle="Checked against the agent's patch when it finishes." />
      <section className="stack min0">
        {item.status === "pending" && (
          <>
            <ReviewThreads ctx={ctx} item={item} />
            <PromptCard {...promptProps} heading="Draft with the agent" action="Draft with agent" primary
              note="This is the prompt the agent gets. Edit it to add what you know: the approach you want, files to leave alone, edge cases to handle." />
          </>
        )}
        {item.status === "drafting" && (
          <div className="card">
            <p className="muted">
              {item.checks_running
                ? "The agent finished. Running lint and tests for the changed files…"
                : "The agent is drafting this item in its own worktree. Checks run when it finishes; this page updates by itself."}
            </p>
          </div>
        )}
        {item.status === "failed" && (
          <>
            <ReviewThreads ctx={ctx} item={item} />
            <PromptCard {...promptProps} heading={<span className="error">The draft failed: {item.error}</span>} action="Try again" primary />
          </>
        )}
        {item.status === "committed" && (
          <div className="card"><p>Committed as <span className="mono">{item.commit_sha?.slice(0, 7)}</span>. It will be pushed from Respond.</p><EditConflicts files={item.edit_conflicts} /></div>
        )}
        {(item.status === "ready" || item.status === "committed") && item.patch && (
          <div className="card flush">
            <div className="card-head">
              <strong>Agent's draft patch</strong>
              <span className="muted small">{files} file{files === 1 ? "" : "s"}</span>
            </div>
            <DiffView text={item.patch} />
          </div>
        )}
        {item.status === "ready" && (
          <>
            {item.agent_log && (
              <details className="card">
                <summary className="small">What the agent says it did</summary>
                <p className="small pre-wrap">{item.agent_log}</p>
              </details>
            )}
            <label className="field">
              Commit message
              <textarea className="input mono" rows={3} value={message} onChange={(e) => setMessage(e.target.value)} />
            </label>
            {failing > 0 && (
              <p className="warn-box small">{failing} check{failing === 1 ? "" : "s"} failed or couldn't run. You can still accept, or ask the agent to fix them first.</p>
            )}
            <div className="row-wrap">
              <button className="btn primary" onClick={() => accept.mutate()} disabled={accept.isPending || !message.trim() || item.checks_running}>Accept and commit</button>
              <span className="muted small">Commits locally on reviewr's branch. Pushed together with the replies in Respond.</span>
            </div>
            {accept.error && <p className="error small">{(accept.error as Error).message}</p>}
            <ReviewThreads ctx={ctx} item={item} collapsed />
            <PromptCard {...promptProps} heading="Ask the agent to change it" action="Redraft with this prompt"
              note="The prompt the last draft used. Add what you want changed (at the end is fine) and redraft; the new draft starts over from the branch." />
          </>
        )}
        {draft.error && <p className="error small">{(draft.error as Error).message}</p>}
      </section>
      {(item.patch || item.check_report) && (
        <aside className="stack side">
          <ChecksPanel item={item} running={checks.isPending} onRun={() => checks.mutate()}
            onFixFailures={item.status === "ready" ? fixFailures : undefined} />
          {checks.error && <p className="error small">{(checks.error as Error).message}</p>}
        </aside>
      )}
    </div>
  );
}

/** The agent's prompt, editable, with the button that drafts from it. */
function PromptCard({ heading, note, text, setText, defaultText, loading, action, primary, busy, onDraft }: {
  heading: ReactNode; note?: string; text: string; setText: (v: string) => void; defaultText?: string; loading: boolean;
  action: string; primary?: boolean; busy: boolean; onDraft: () => void;
}) {
  const edited = defaultText !== undefined && text.trim() !== defaultText.trim();
  return (
    <section className="card stack-gap">
      <h2 className="h3">{heading}</h2>
      {note && <p className="muted small">{note}</p>}
      <div className="row-between">
        <span className="small strong">Agent prompt {edited && <span className="pill">Edited</span>}</span>
        {edited && <button className="link small" onClick={() => setText(defaultText!)}>Reset to the generated prompt</button>}
      </div>
      <textarea className="input mono prompt-edit" rows={Math.min(28, Math.max(10, text.split("\n").length + 1))}
        value={loading ? "Loading the prompt…" : text} disabled={loading} onChange={(e) => setText(e.target.value)}
        aria-label="Agent prompt" />
      <div className="row-wrap">
        <button className={`btn ${primary ? "primary" : ""}`} onClick={onDraft} disabled={busy || loading || !text.trim()}>{action}</button>
        {edited && <span className="muted small">Your edited prompt is saved with this item when you draft.</span>}
      </div>
    </section>
  );
}

/** The review threads this item addresses, in full: the conversation, the code it's on,
 *  and your note from Triage. */
function ReviewThreads({ ctx, item, collapsed }: { ctx: PRContext; item: WorkItem; collapsed?: boolean }) {
  const { pr, state } = ctx.data!;
  const threads = item.thread_ids.map((id) => pr.threads.find((t) => t.id === id)).filter((t): t is Thread => !!t);
  const body = (
    <div className="stack-gap">
      {threads.map((t) => {
        const note = state.decisions[t.id]?.note;
        const code = t.diff_hunk.trim().split("\n").slice(-16).join("\n");
        return (
          <div key={t.id} className="review-thread stack-gap">
            <div className="row-wrap small">
              <strong className="grow">{threadTitle(t, state)}</strong>
              <a href={t.comments[0]?.url} target="_blank" rel="noreferrer">View on GitHub</a>
            </div>
            <div className="mono muted tiny">{where(t)}</div>
            {code && <DiffView text={code} />}
            <div className="conversation">
              {t.comments.map((c) => (
                <div key={c.id} className="quote">
                  <div className="muted tiny">{c.author} · {new Date(c.created_at).toLocaleString()}</div>
                  <Markdown text={c.body} />
                </div>
              ))}
            </div>
            {note && <div className="suggest small"><strong>Your note from Triage:</strong> {note}</div>}
          </div>
        );
      })}
    </div>
  );
  const title = `What the reviewer${threads.length === 1 ? "" : "s"} asked · ${threads.length} thread${threads.length === 1 ? "" : "s"}`;
  return collapsed ? (
    <details className="card"><summary className="strong small">{title}</summary><div className="pad-top">{body}</div></details>
  ) : (
    <section className="card stack-gap"><h2 className="h3">{title}</h2>{body}</section>
  );
}

function OwnItem({ ctx, item }: { ctx: PRContext; item: WorkItem }) {
  const { pr } = ctx.data!;
  const [message, setMessage] = useState(defaultMessage(item, useReviewers(ctx, item)));
  const diff = useQuery({
    queryKey: ["workdiff", pr.ref.number],
    queryFn: () => api.workDiff(pr.ref),
    refetchInterval: item.status === "committed" ? false : 5000,
  });
  const check = useMutation({ mutationFn: () => api.check(pr.ref, item.id), onSuccess: ctx.setState });
  // Only the files you tick are committed; anything else you have uncommitted stays put.
  const changedFiles = [...new Set([...(diff.data?.diff ?? "").matchAll(/^diff --git a\/.+? b\/(.+)$/gm)].map((m) => m[1]))];
  const itemFiles = new Set(item.thread_ids.map((id) => pr.threads.find((t) => t.id === id)?.path));
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const isPicked = (f: string) => picked[f] ?? itemFiles.has(f);
  const chosen = changedFiles.filter(isPicked);
  const commit = useMutation({ mutationFn: () => api.commitOwn(pr.ref, item.id, message, chosen), onSuccess: ctx.setState });
  const runChecks = useMutation({ mutationFn: () => api.runChecks(pr.ref, item.id), onSuccess: ctx.setState });
  const fix = item.proposed_fix;
  const fixPending = fix?.status === "running" || fix?.status === "ready";
  const askFix = useMutation({
    mutationFn: ({ failure, label }: { failure: string; label: string }) => api.ownFix(pr.ref, item.id, failure, label),
    onSuccess: ctx.setState,
  });
  const applyFix = useMutation({
    mutationFn: () => api.ownFixApply(pr.ref, item.id),
    onSuccess: (st) => { ctx.setState(st); diff.refetch(); },
  });
  const discardFix = useMutation({ mutationFn: () => api.ownFixDiscard(pr.ref, item.id), onSuccess: ctx.setState });
  const retryFix = useMutation({
    mutationFn: () => api.ownFixDiscard(pr.ref, item.id).then(() => api.ownFix(pr.ref, item.id, fix!.failure, fix!.label)),
    onSuccess: ctx.setState,
  });

  const changed = (diff.data?.diff.match(/^diff --git/gm) ?? []).length;

  return (
    <div className="fix-layout">
      <ThreadChecks ctx={ctx} item={item} subtitle="Use “Check against threads” after you make changes." />
      <section className="stack min0">
        {item.status === "committed" ? (
          <div className="card"><p>Committed as <span className="mono">{item.commit_sha?.slice(0, 7)}</span>. It will be pushed from Respond.</p><EditConflicts files={item.edit_conflicts} /></div>
        ) : (
          <>
            {fix && (
              <ProposedFix
                fix={fix} where="a copy of your changes" onWhat="on top of your uncommitted changes" applyLabel="Apply to my checkout"
                applyNote="Applying writes them into your checkout as uncommitted edits, so you can review or change them before committing."
                onApply={() => applyFix.mutate()} onDiscard={() => discardFix.mutate()} onRetry={() => retryFix.mutate()}
                busy={applyFix.isPending || discardFix.isPending || retryFix.isPending}
                error={(applyFix.error || discardFix.error || retryFix.error) as Error | null}
              />
            )}
            <ReviewThreads ctx={ctx} item={item} />
            <p className="muted small">
              Edit in the PR's workspace (open it from Workspace in the header), from VS Code or anywhere else. Your uncommitted changes there show up below, and Commit makes them this item's commit.
            </p>
            <div className="card flush">
              <div className="card-head">
                <strong>Your uncommitted changes</strong>
                <span className="muted small">{changed} file{changed === 1 ? "" : "s"} · refreshes every 5 seconds</span>
              </div>
              {diff.error && <p className="error small pad">{(diff.error as Error).message}</p>}
              {diff.data && (diff.data.diff ? <DiffView text={diff.data.diff} /> : <p className="muted small pad">No changes yet.</p>)}
            </div>
            {changedFiles.length > 0 && (
              <fieldset className="card file-pick">
                <legend className="small strong">Files in this commit</legend>
                <p className="muted tiny">Files the item's comments point at start ticked. Other uncommitted files in your checkout stay out unless you tick them.</p>
                {changedFiles.map((f) => (
                  <label key={f} className="check-row small">
                    <input type="checkbox" checked={isPicked(f)} onChange={(e) => setPicked({ ...picked, [f]: e.target.checked })} />
                    <span className="mono">{f}</span>
                    {itemFiles.has(f) && <span className="muted tiny">· from this item</span>}
                  </label>
                ))}
              </fieldset>
            )}
            <label className="field">
              Commit message
              <textarea className="input mono" rows={3} value={message} onChange={(e) => setMessage(e.target.value)} />
            </label>
            <div className="row-wrap">
              <button className="btn" onClick={() => check.mutate()} disabled={check.isPending || !diff.data?.diff}>
                {check.isPending ? "Checking…" : "Check against threads"}
              </button>
              <button className="btn primary" onClick={() => commit.mutate()} disabled={commit.isPending || !chosen.length || !message.trim()}>
                Commit {chosen.length} file{chosen.length === 1 ? "" : "s"}
              </button>
              <span className="muted small">Commits stay local until Respond.</span>
            </div>
            {(check.error || commit.error) && <p className="error small">{((check.error || commit.error) as Error).message}</p>}
          </>
        )}
      </section>
      <aside className="stack side">
        <ChecksPanel item={item} running={runChecks.isPending} onRun={() => runChecks.mutate()}
          onFixFailures={item.status !== "committed" && !fixPending ? (failure, label) => askFix.mutate({ failure, label }) : undefined} />
        {(runChecks.error || askFix.error) && <p className="error small">{((runChecks.error || askFix.error) as Error).message}</p>}
      </aside>
    </div>
  );
}
