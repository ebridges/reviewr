import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { PRContext } from "../App";
import { api, type Thread } from "../api";
import { decisionLabel, excerpt, threadTitle, where } from "../lib";

export function Respond({ ctx }: { ctx: PRContext }) {
  const { pr, state } = ctx.data!;
  const qc = useQueryClient();
  const sub = useQuery({ queryKey: ["submission", pr.ref.number, state], queryFn: () => api.submission(pr.ref) });
  const [summary, setSummary] = useState<string>();
  const [confirming, setConfirming] = useState(false);
  useEffect(() => { if (sub.data && summary === undefined) setSummary(sub.data.summary); }, [sub.data, summary]);

  // "No action" threads get no reply, so they don't appear here.
  const decided = pr.threads.filter((t) => !t.is_resolved && state.decisions[t.id] && state.decisions[t.id].decision !== "no_action");
  const reviewers = [...new Set(decided.map((t) => t.comments[0]?.author ?? "ghost"))];
  const [tab, setTab] = useState<string>();
  const activeTab = tab ?? reviewers[0];
  const missing = decided.filter((t) => !state.decisions[t.id].reply.trim()).length;

  const draft = useMutation({ mutationFn: () => api.draftReplies(pr.ref), onSuccess: ctx.setState });

  const noActionOnly = decided.length === 0 && Object.values(state.decisions).some((d) => d.decision === "no_action");
  const submit = useMutation({
    mutationFn: () => api.submit(pr.ref),
    onSuccess: () => { setConfirming(false); qc.invalidateQueries({ queryKey: ["pr"] }); },
  });
  const saveSummary = () => { if (summary !== undefined && summary !== sub.data?.summary) api.setSummary(pr.ref, summary).then(ctx.setState); };

  const s = sub.data;
  const resolveCount = s?.replies.filter((r) => r.resolve).length ?? 0;

  return (
    <main className="page narrow">
      <div className="page-head">
        <div className="grow">
          <h1>{decided.length} draft replies</h1>
          <p className="muted">Written from your decisions and commits: one or two sentences each, with the commit where there is one. Nothing is posted until you submit.</p>
        </div>
        {missing > 0 && (
          <button className="btn" onClick={() => draft.mutate()} disabled={draft.isPending}>
            {draft.isPending ? "Drafting…" : `Draft ${missing} missing repl${missing === 1 ? "y" : "ies"}`}
          </button>
        )}
      </div>
      {draft.error && <div className="banner error">{(draft.error as Error).message}</div>}
      {state.submitted_at && <div className="banner">Submitted {new Date(state.submitted_at).toLocaleString()}.</div>}

      <section className="card stack-gap">
        <div className="row-between">
          <h2>Summary comment</h2>
          <span className="muted small">Posted at the top of the review.</span>
        </div>
        <textarea className="input mono" rows={9} value={summary ?? ""} onChange={(e) => setSummary(e.target.value)} onBlur={saveSummary} />
      </section>

      <p className="muted small">Fixed and already-done threads are resolved when the reply posts. Answers and push-backs stay open for the reviewer to resolve.</p>

      <div role="tablist" className="tabs">
        {reviewers.map((r) => (
          <button key={r} role="tab" aria-selected={r === activeTab} className={r === activeTab ? "on" : ""} onClick={() => setTab(r)}>
            {r} · {decided.filter((t) => (t.comments[0]?.author ?? "ghost") === r).length}
          </button>
        ))}
      </div>

      {decided.filter((t) => (t.comments[0]?.author ?? "ghost") === activeTab).map((t) => <ReplyCard key={t.id} ctx={ctx} thread={t} />)}
      {decided.length === 0 && (
        <p className="muted">
          {noActionOnly
            ? "Nothing to post: every decided thread is No action, so there are no replies to write."
            : "No decisions yet. Decide threads in Triage first."}
        </p>
      )}

      <footer className="submit-bar">
        {s && (
          <span className="grow small">
            <strong>{s.replies.length} replies</strong>, <strong>{resolveCount} resolved</strong>, posted as one review.
            {s.commits_to_push > 0 && <> Pushes {s.commits_to_push} commit{s.commits_to_push === 1 ? "" : "s"} to <span className="mono">{s.branch}</span> first.</>}
            {s.rerequest.length > 0 && <> Re-requests review from {s.rerequest.join(", ")}.</>}
            {s.missing_replies.length > 0 && <span className="error"> {s.missing_replies.length} replies are still empty.</span>}
          </span>
        )}
        <button className="btn primary" disabled={!s || !s.replies.length || s.missing_replies.length > 0} onClick={() => setConfirming(true)}>
          Review and submit
        </button>
      </footer>

      {confirming && s && (
        <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="confirm-title">
          <div className="modal">
            <h2 id="confirm-title">Post to GitHub?</h2>
            <p className="small">This will, in order:</p>
            <ol className="small">
              {s.commits_to_push > 0 && <li>Push {s.commits_to_push} commits to <span className="mono">{s.branch}</span> (fast-forward only)</li>}
              <li>Post a review with the summary comment and {s.replies.length} replies</li>
              {resolveCount > 0 && <li>Resolve {resolveCount} threads</li>}
              {s.rerequest.length > 0 && <li>Re-request review from {s.rerequest.join(", ")}</li>}
            </ol>
            <p className="muted small">Reviewers can see all of this as soon as it's posted. Replies go out one per second.</p>
            {submit.error && <p className="error small">{(submit.error as Error).message}</p>}
            {submit.data && <ul className="small">{submit.data.done.map((d) => <li key={d}>{d}</li>)}</ul>}
            <div className="row-wrap end">
              <button className="btn" onClick={() => setConfirming(false)}>{submit.data ? "Close" : "Cancel"}</button>
              {!submit.data && (
                <button className="btn primary" onClick={() => submit.mutate()} disabled={submit.isPending}>
                  {submit.isPending ? "Posting…" : "Push and submit review"}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

function ReplyCard({ ctx, thread }: { ctx: PRContext; thread: Thread }) {
  const { pr, state } = ctx.data!;
  const d = state.decisions[thread.id];
  const [text, setText] = useState(d.reply);
  useEffect(() => setText(d.reply), [d.reply]);
  const first = thread.comments[0];

  return (
    <section className="card stack-gap">
      <div className="row-wrap">
        <span className="chip">{decisionLabel(d.decision)}</span>
        <strong className="grow small">{threadTitle(thread, state)}</strong>
        <span className="mono muted tiny">{where(thread)}</span>
      </div>
      <p className="muted small clamp">{first?.author}: {excerpt(first?.body ?? "", 400)}</p>
      <textarea className="input" rows={2} aria-label="Reply" value={text} placeholder="No reply yet"
        onChange={(e) => setText(e.target.value)}
        onBlur={() => text !== d.reply && api.patchDecision(pr.ref, thread.id, { reply: text }).then(ctx.setState)} />
      <label className="check-row small">
        <input type="checkbox" checked={d.resolve} onChange={(e) => api.patchDecision(pr.ref, thread.id, { resolve: e.target.checked }).then(ctx.setState)} />
        Resolve thread after replying
      </label>
    </section>
  );
}
