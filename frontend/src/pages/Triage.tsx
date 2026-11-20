import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import type { PRContext } from "../App";
import { api, type Decision, type ReviewState, type Thread } from "../api";
import { Snippet, DiffView } from "../components/Code";
import { Dot, FoldButton, useFolded } from "../components/Fold";
import { KeyHelp } from "../components/KeyHelp";
import { Markdown } from "../components/Markdown";
import { DECISIONS, DECISION_HELP, GROUP_LABEL, decisionLabel, excerpt, prHref, storageGet, storageSet, threadTitle, useAIName, where } from "../lib";

const REPLY_DECISIONS: Decision[] = ["reply", "push_back", "defer"];
// Push back needs your reason first; Claude can't know why you disagree.
const AUTO_DRAFT: Decision[] = ["reply", "defer"];
type Show = "undecided" | "all" | "decided";

interface Group { key: string; label: string; decision?: Decision; threads: Thread[] }

// Stale threads stay together. Every other thread is grouped by your decision if you've
// made one, otherwise by Claude's suggestion, so a thread you push back on moves to
// "Push back". Empty groups are hidden.
function groupThreads(open: Thread[], state: ReviewState): Group[] {
  const groups: Group[] = [{ key: "stale", label: "Stale", threads: open.filter((t) => t.is_outdated) }];
  const groupOf = (t: Thread) => state.decisions[t.id]?.decision ?? state.suggestions[t.id]?.decision;
  for (const d of ["fix", "reply", "push_back", "defer", "already_done", "no_action"] as Decision[]) {
    groups.push({
      key: d, label: GROUP_LABEL[d], decision: d,
      threads: open.filter((t) => !t.is_outdated && groupOf(t) === d),
    });
  }
  groups.push({ key: "none", label: "Not yet suggested", threads: open.filter((t) => !t.is_outdated && !groupOf(t)) });
  return groups.filter((g) => g.threads.length);
}

export function Triage({ ctx, arg }: { ctx: PRContext; arg?: string }) {
  const { pr, state } = ctx.data!;
  const aiName = useAIName();
  const open = useMemo(() => pr.threads.filter((t) => !t.is_resolved), [pr]);
  const [view, setView] = useState<"card" | "list">((storageGet("reviewr:triageView") as "card" | "list") ?? "card");
  const [show, setShow] = useState<Show>("undecided");
  const groups = useMemo(() => groupThreads(open, state), [open, state]);
  const ordered = groups.flatMap((g) => g.threads);
  const selectedId = arg ? decodeURIComponent(arg) : ordered.find((t) => !state.decisions[t.id])?.id ?? ordered[0]?.id;
  const selected = open.find((t) => t.id === selectedId);

  const select = (id: string) => { location.hash = prHref(pr.ref, "triage", encodeURIComponent(id)); };
  // Put the default selection in the URL, so deciding a thread doesn't move the card
  // to whichever thread becomes "first undecided".
  useEffect(() => {
    if (!arg && selectedId) history.replaceState(null, "", prHref(pr.ref, "triage", encodeURIComponent(selectedId)));
  }, [arg, selectedId, pr.ref]);
  const setViewSaved = (v: "card" | "list") => { setView(v); storageSet("reviewr:triageView", v); };
  const visible = (t: Thread) => show === "all" || (show === "decided") === !!state.decisions[t.id];
  const nextUndecided = (after: string) => {
    const i = ordered.findIndex((t) => t.id === after);
    return [...ordered.slice(i + 1), ...ordered.slice(0, i)].find((t) => !state.decisions[t.id] && t.id !== after);
  };

  const noSuggestions = Object.keys(state.suggestions).length === 0;
  const queueFold = useFolded("triage-queue");

  return (
    <div className="triage">
      <aside className="queue" aria-label="Thread queue">
        <div className="queue-controls">
          <div className="seg" role="group" aria-label="View">
            <button className={view === "card" ? "on" : ""} onClick={() => setViewSaved("card")}>Card</button>
            <button className={view === "list" ? "on" : ""} onClick={() => setViewSaved("list")}>List</button>
          </div>
          <select className="input" value={show} onChange={(e) => setShow(e.target.value as Show)} aria-label="Show">
            <option value="undecided">Undecided ({open.filter((t) => !state.decisions[t.id]).length})</option>
            <option value="all">All ({open.length})</option>
            <option value="decided">Decided ({open.filter((t) => state.decisions[t.id]).length})</option>
          </select>
        </div>
        {noSuggestions && (
          <p className="muted small pad">
            No suggestions yet. <a href={prHref(pr.ref)}>Ask {aiName} on the Overview</a> to group threads by the decision they need.
          </p>
        )}
        {groups.map((g) => {
          const all = g.threads.filter(visible);
          if (!all.length) return null;
          const folded = queueFold.isFolded(g.key);
          // A folded group still shows the thread that's open in the card.
          const rows = folded ? all.filter((t) => t.id === selected?.id && view === "card") : all;
          return (
            <div key={g.key}>
              <div className="queue-group">
                <FoldButton folded={folded} onClick={() => queueFold.toggle(g.key)} label={g.label} />
                <span className="grow row-wrap group-head-click" onClick={() => queueFold.toggle(g.key)}><Dot kind={g.key} />{g.label} · {g.threads.length}</span>
                {g.decision && view === "card" && (
                  <button className="link small" onClick={() => setViewSaved("list")}>Mark all</button>
                )}
              </div>
              {rows.map((t) => {
                const d = state.decisions[t.id];
                return (
                  <button key={t.id} className={`qrow ${t.id === selected?.id && view === "card" ? "sel" : ""}`} onClick={() => { setViewSaved("card"); select(t.id); }}>
                    <span className="mark" aria-hidden="true">{d ? "✓" : ""}</span>
                    <span className="grow min0">
                      <span className="truncate block">{threadTitle(t, state)}</span>
                      <span className="mono muted tiny">{where(t)}</span>
                      {d && <span className="tiny decided-tag"><Dot kind={d.decision} /> {decisionLabel(d.decision)}</span>}
                    </span>
                  </button>
                );
              })}
            </div>
          );
        })}
      </aside>

      <main className="triage-main">
        {view === "card" && selected && (
          <ThreadCard key={selected.id} ctx={ctx} thread={selected} onDone={() => {
            const n = nextUndecided(selected.id);
            if (n) select(n.id);
          }} onNav={(dir) => {
            const i = ordered.findIndex((t) => t.id === selected.id);
            const n = ordered[i + dir];
            if (n) select(n.id);
          }} />
        )}
        {view === "card" && !selected && <p className="muted">No open threads.</p>}
        {view === "list" && <ThreadList ctx={ctx} groups={groups} visible={visible} onOpen={(id) => { setViewSaved("card"); select(id); }} />}
      </main>
    </div>
  );
}

function useEditor() {
  const config = useQuery({ queryKey: ["config"], queryFn: () => fetch("/api/config").then((r) => r.json()) });
  return storageGet("reviewr:editor") || config.data?.default_editor || "vscode";
}

function ThreadCard({ ctx, thread, onDone, onNav }: { ctx: PRContext; thread: Thread; onDone: () => void; onNav: (dir: number) => void }) {
  const { pr, state } = ctx.data!;
  const aiName = useAIName();
  const s = state.suggestions[thread.id];
  const d = state.decisions[thread.id];
  const editor = useEditor();
  const [note, setNote] = useState(d?.note ?? "");
  const [status, setStatus] = useState<string>();
  const undecidedSimilar = (s?.similar ?? []).filter((id) => pr.threads.some((t) => t.id === id && !t.is_resolved));
  const [similarChecked, setSimilarChecked] = useState<Set<string>>(new Set(undecidedSimilar.filter((id) => !state.decisions[id])));
  const trace = useQuery({ queryKey: ["trace", thread.id], queryFn: () => api.trace(pr.ref, thread.id), enabled: thread.is_outdated });

  const draft = useMutation({
    mutationFn: (ids: string[]) => api.draftReplies(pr.ref, ids),
    onSuccess: ctx.setState,
  });
  const decide = useMutation({
    mutationFn: async (decision: Decision) => {
      const ids = [thread.id, ...similarChecked];
      return { st: await api.decide(pr.ref, ids, decision, note || undefined), ids };
    },
    onSuccess: ({ st, ids }, decision) => {
      ctx.setState(st);
      if (AUTO_DRAFT.includes(decision)) draft.mutate(ids);
      else if (decision === "push_back") { /* the reply composer asks for the reason */ }
      else onDone();
    },
  });

  const openEditor = async () => {
    try {
      const line = thread.line ?? trace.data?.current_line ?? thread.original_line;
      const path = trace.data?.current_path ?? thread.path;
      const res = await api.open(pr.ref, { path, line, editor });
      setStatus(`Opened: ${res.argv.join(" ")}`);
    } catch (e) {
      setStatus((e as Error).message);
    }
  };

  // Keyboard shortcuts. Ignored while typing in a field.
  const handler = useRef<(e: KeyboardEvent) => void>();
  handler.current = (e: KeyboardEvent) => {
    const tag = (e.target as HTMLElement).tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.metaKey || e.ctrlKey) return;
    const k = e.key.toLowerCase();
    const dec = DECISIONS.find((x) => x.shortcut === k);
    if (dec) { e.preventDefault(); decide.mutate(dec.key); }
    else if (k === "enter" && s) { e.preventDefault(); decide.mutate(s.decision); }
    else if (k === "j") onNav(1);
    else if (k === "k") onNav(-1);
    else if (k === "e") openEditor();
    else if (k === "z" && d) api.undecide(pr.ref, thread.id).then(ctx.setState);
  };
  useEffect(() => {
    const on = (e: KeyboardEvent) => handler.current?.(e);
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  }, []);

  const saveNote = () => { if (d && note !== d.note) api.patchDecision(pr.ref, thread.id, { note }).then(ctx.setState); };
  const reviewer = thread.comments[0]?.author ?? "the reviewer";
  const t = trace.data;

  return (
    <div className="triage-card-wrap">
      <article className="card stack-gap">
        <div>
          <div className="card-top">
            <div className="muted small row-wrap grow">
              {thread.is_outdated && <span className="pill warn">Stale</span>}
              {reviewer} · <span className="mono">{where(thread)}</span> ·
              <a href={thread.comments[0]?.url} target="_blank" rel="noreferrer">View on GitHub</a> ·
              <button className="link" onClick={openEditor}>Open in {editor}</button>
            </div>
            <KeyHelp>
              <div><span className="kbd">F R P A D</span> decide</div>
              <div><span className="kbd">Enter</span> use {aiName}'s suggestion</div>
              <div><span className="kbd">J</span> / <span className="kbd">K</span> next / previous</div>
              <div><span className="kbd">E</span> open in editor</div>
              <div><span className="kbd">Z</span> clear this decision</div>
            </KeyHelp>
          </div>
          <h1 className="h-thread">{threadTitle(thread, state)}</h1>
          {status && <div className="muted tiny">{status}</div>}
        </div>

        <div className="conversation">
          {thread.comments.map((c) => (
            <div key={c.id} className="quote">
              <div className="muted tiny">{c.author} · {new Date(c.created_at).toLocaleString()}</div>
              <Markdown text={c.body} />
            </div>
          ))}
        </div>

        {!thread.is_outdated && (
          <div>
            <div className="label">What {reviewer} saw</div>
            <DiffView text={thread.diff_hunk.split("\n").slice(-12).join("\n")} />
          </div>
        )}

        {thread.is_outdated && (
          <section className="trace">
            <div className="label">Where the comment pointed</div>
            {trace.isLoading && <p className="muted small">Tracing through git history…</p>}
            {trace.error && <p className="error small">{(trace.error as Error).message}</p>}
            {t && (
              <>
                <p className="small">
                  Originally <span className="mono">{t.original_path}:{t.original_line}</span> at{" "}
                  <span className="mono">{t.original_commit?.slice(0, 7)}</span>.{" "}
                  {t.status === "deleted" && "That file no longer exists at the PR head."}
                  {t.status === "edited" && <>Those lines were <strong>edited</strong>; they're now near <span className="mono">{t.current_path}:{t.current_line}</span>.</>}
                  {t.status === "moved" && <>The lines <strong>moved</strong> to <span className="mono">{t.current_path}:{t.current_line}</span> but weren't changed.</>}
                  {t.status === "unchanged" && "The lines are unchanged at the same place."}
                  {t.status === "unknown" && "The original commit isn't in your local clone, so it can't be traced."}
                </p>
                {t.commits.length > 0 && (
                  <details>
                    <summary className="small">{t.commits.length} commit{t.commits.length === 1 ? "" : "s"} changed this file since the comment</summary>
                    <ul className="commits">
                      {t.commits.slice(0, 20).map((c) => <li key={c.sha}><span className="mono">{c.sha}</span> {c.subject}</li>)}
                    </ul>
                  </details>
                )}
                <div className="two-col">
                  <div>
                    <div className="label">When {reviewer} commented</div>
                    <Snippet text={t.original_snippet} start={t.snippet_start_original} highlight={t.original_line} />
                  </div>
                  <div>
                    <div className="label">Now</div>
                    <Snippet text={t.current_snippet} start={t.snippet_start_current} highlight={t.current_line} />
                  </div>
                </div>
              </>
            )}
          </section>
        )}

        {s && (
          <div className={`suggest ${s.stale_verdict === "not_addressed" ? "warn" : ""}`}>
            <div className="strong small row-wrap"><Dot kind={s.decision} />{aiName} suggests: {decisionLabel(s.decision)}</div>
            <p className="small">{s.reason}</p>
            {s.stale_verdict && (
              <p className="small muted">
                Check of the current code: {s.stale_verdict === "addressed" ? "looks addressed" : s.stale_verdict === "not_addressed" ? "not addressed" : "can't tell"}.
              </p>
            )}
          </div>
        )}

        <div>
          <div className="label">Decision {d && <span className="muted">· currently {decisionLabel(d.decision)}</span>}</div>
          <div className="row-wrap">
            {DECISIONS.map((x) => (
              <button
                key={x.key}
                className={`btn ${d?.decision === x.key ? "primary" : s?.decision === x.key && !d ? "suggested" : ""}`}
                onClick={() => decide.mutate(x.key)}
                disabled={decide.isPending}
              >
                <span className="kbd">{x.shortcut.toUpperCase()}</span>{x.label}
              </button>
            ))}
            {d && <button className="btn" onClick={() => api.undecide(pr.ref, thread.id).then(ctx.setState)}>Clear</button>}
          </div>
          {decide.error && <p className="error small">{(decide.error as Error).message}</p>}
          {(d || s) && (
            <p className="muted small decision-help">
              {d ? <strong>{decisionLabel(d.decision)}: </strong> : <>If you choose <strong>{decisionLabel(s!.decision)}</strong>: </>}
              {DECISION_HELP[(d ?? s)!.decision].replace("{ai}", aiName)}
            </p>
          )}
        </div>

        {d && REPLY_DECISIONS.includes(d.decision) ? (
          <ReplyComposer ctx={ctx} thread={thread} drafting={draft.isPending} draftError={draft.error} onNext={onDone} />
        ) : (
          <label className="field">
            Note (optional, used when drafting the fix or the reply)
            <input className="input" value={note} onChange={(e) => setNote(e.target.value)} onBlur={saveNote} placeholder="e.g. keep the old helper until the migration lands" />
          </label>
        )}
      </article>

      {undecidedSimilar.length > 0 && (
      <aside className="stack">
          <section className="card">
            <h2>Same request elsewhere</h2>
            <p className="muted small">Your decision also applies to the checked threads.</p>
            {undecidedSimilar.map((id) => {
              const other = pr.threads.find((x) => x.id === id)!;
              return (
                <label key={id} className="check-row">
                  <input type="checkbox" checked={similarChecked.has(id)} onChange={(e) => {
                    const next = new Set(similarChecked);
                    if (e.target.checked) next.add(id); else next.delete(id);
                    setSimilarChecked(next);
                  }} />
                  <span>
                    <span className="block small">{threadTitle(other, state)}</span>
                    <span className="mono muted tiny">{where(other)}{state.decisions[id] ? ` · ${decisionLabel(state.decisions[id].decision)}` : ""}</span>
                  </span>
                </label>
              );
            })}
          </section>
      </aside>
      )}
    </div>
  );
}

function ThreadList({ ctx, groups, visible, onOpen }: { ctx: PRContext; groups: Group[]; visible: (t: Thread) => boolean; onOpen: (id: string) => void }) {
  const { pr, state } = ctx.data!;
  const aiName = useAIName();
  const listFold = useFolded("triage-list");
  const [selected, setSelected] = useState<Record<string, Set<string>>>({});
  const [busy, setBusy] = useState<string>();
  const [error, setError] = useState<string>();

  const sel = (g: Group) => selected[g.key] ?? new Set(g.threads.filter((t) => !state.decisions[t.id]).map((t) => t.id));
  const toggle = (g: Group, id: string, on: boolean) => {
    const next = new Set(sel(g));
    if (on) next.add(id); else next.delete(id);
    setSelected({ ...selected, [g.key]: next });
  };

  const run = async (key: string, fn: () => Promise<ReviewState>) => {
    setBusy(key); setError(undefined);
    try { ctx.setState(await fn()); } catch (e) { setError((e as Error).message); } finally { setBusy(undefined); }
  };

  const decideRow = (t: Thread, decision: Decision) =>
    run(t.id, async () => {
      const st = await api.decide(pr.ref, [t.id], decision);
      return AUTO_DRAFT.includes(decision) ? api.draftReplies(pr.ref, [t.id]) : st;
    });

  const markGroup = (g: Group) => {
    const ids = [...sel(g)];
    if (!ids.length || !g.decision) return;
    run(g.key, async () => {
      const st = await api.decide(pr.ref, ids, g.decision!);
      return AUTO_DRAFT.includes(g.decision!) ? api.draftReplies(pr.ref, ids) : st;
    });
  };

  // Undo decisions: the selected decided threads, or every decided thread shown in the
  // group when none of them is selected. Drafted replies for them are discarded.
  const clearGroup = (g: Group, ids: string[]) => {
    if (!ids.length) return;
    if (!confirm(`Clear ${ids.length} decision${ids.length === 1 ? "" : "s"}? The thread${ids.length === 1 ? " goes" : "s go"} back to undecided, and any drafted replies are discarded.`)) return;
    run(`clear-${g.key}`, async () => {
      const st = await api.clearDecisions(pr.ref, ids);
      setSelected((prev) => { const next = { ...prev }; delete next[g.key]; return next; });
      return st;
    });
  };

  return (
    <div className="stack">
      <p className="muted small">
        Marking only sets each thread's decision. No code changes and nothing is posted; you can change any of them later.
      </p>
      {error && <div className="banner error">{error}</div>}
      {groups.map((g) => {
        const rows = g.threads.filter(visible);
        if (!rows.length) return null;
        const s = sel(g);
        const decidedRows = rows.filter((t) => state.decisions[t.id]).map((t) => t.id);
        const selectedDecided = [...s].filter((id) => state.decisions[id]);
        const toClear = selectedDecided.length ? selectedDecided : decidedRows;
        return (
          <section key={g.key} className={`card list dc-${g.key}`}>
            <div className="list-head dc-edge">
              <FoldButton folded={listFold.isFolded(g.key)} onClick={() => listFold.toggle(g.key)} label={g.label} />
              <input type="checkbox" aria-label="Select all" checked={rows.every((t) => s.has(t.id))}
                onChange={(e) => setSelected({ ...selected, [g.key]: e.target.checked ? new Set(rows.map((t) => t.id)) : new Set() })} />
              <h2 className="grow row-wrap group-head-click" onClick={() => listFold.toggle(g.key)}><Dot kind={g.key} />{g.label} · {g.threads.length}{g.key === "stale" && <span className="muted small"> · decide each one after seeing where it pointed</span>}</h2>
              {g.decision ? (
                <button className="btn sm" disabled={!s.size || busy === g.key} onClick={() => markGroup(g)}>
                  {busy === g.key ? "Marking…" : `Mark ${s.size} as ${decisionLabel(g.decision)}`}
                </button>
              ) : g.key === "stale" ? <span className="muted small">Stale threads can't be marked in bulk</span> : null}
              {decidedRows.length > 0 && (
                <button className="btn sm" disabled={busy === `clear-${g.key}`} onClick={() => clearGroup(g, toClear)}>
                  {busy === `clear-${g.key}` ? "Clearing…" : selectedDecided.length ? `Clear ${toClear.length} decision${toClear.length === 1 ? "" : "s"}` : `Clear all ${toClear.length} decision${toClear.length === 1 ? "" : "s"}`}
                </button>
              )}
            </div>
            {!listFold.isFolded(g.key) && rows.map((t) => {
              const d = state.decisions[t.id];
              const sg = state.suggestions[t.id];
              return (
                <div key={t.id} className="list-row-wrap">
                  <div className="list-row">
                    <input type="checkbox" aria-label="Select thread" checked={s.has(t.id)} onChange={(e) => toggle(g, t.id, e.target.checked)} />
                    <div className="grow min0">
                      <div className="truncate">{threadTitle(t, state)}</div>
                      <div className="mono muted tiny">{where(t)}</div>
                    </div>
                    {sg && <span className={`chip dc-chip dc-${sg.decision}`}>{aiName} suggests: {decisionLabel(sg.decision)}</span>}
                    {g.key === "stale" ? (
                      <button className="btn sm" onClick={() => onOpen(t.id)}>{d ? decisionLabel(d.decision) : "See trace"}</button>
                    ) : (
                      <div className="dec" role="group" aria-label="Decision">
                        {DECISIONS.map((x) => (
                          <button key={x.key} title={x.label} aria-pressed={d?.decision === x.key} className={d?.decision === x.key ? "on" : ""}
                            disabled={busy === t.id} onClick={() => decideRow(t, x.key)}>
                            {x.shortcut.toUpperCase()}
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                  {d && REPLY_DECISIONS.includes(d.decision) && g.key !== "stale" && (
                    <div className="reply-inline"><ReplyComposer ctx={ctx} thread={t} compact /></div>
                  )}
                </div>
              );
            })}
          </section>
        );
      })}
    </div>
  );
}

const REASON_PROMPT: Partial<Record<Decision, { label: string; placeholder: string; required: boolean }>> = {
  push_back: {
    label: "Why should the code stay as it is?",
    placeholder: "e.g. The anchor year is already applied before this point, so the base margins are preserved.",
    required: true,
  },
  reply: { label: "What should the answer say? (optional)", placeholder: "e.g. 5 retries is what the provider recommends", required: false },
  defer: { label: "Anything to add, such as a ticket? (optional)", placeholder: "e.g. tracked in MOS-4410", required: false },
};

/**
 * One text box at a time. First (Push back always; Reply only and Defer when you add
 * guidance): your reason or guidance for Claude. Then: the reply that will be posted,
 * with the reason shown as one line you can go back and edit. Cancel clears the
 * decision and its draft, so the thread is undecided again.
 */
function ReplyComposer({ ctx, thread, drafting = false, draftError, onNext, compact = false }: {
  ctx: PRContext; thread: Thread; drafting?: boolean; draftError?: unknown; onNext?: () => void; compact?: boolean;
}) {
  const { pr, state } = ctx.data!;
  const aiName = useAIName();
  const d = state.decisions[thread.id];
  const prompt = REASON_PROMPT[d.decision]!;
  const reviewer = thread.comments[0]?.author ?? "the reviewer";
  const [reason, setReason] = useState(d.note);
  const [reply, setReply] = useState(d.reply);
  const [editingReason, setEditingReason] = useState(!d.reply && prompt.required);
  const draft = useMutation({
    mutationFn: async () => {
      if (reason !== d.note) ctx.setState(await api.patchDecision(pr.ref, thread.id, { note: reason }));
      return api.draftReplies(pr.ref, [thread.id]);
    },
    onSuccess: (st) => { ctx.setState(st); setEditingReason(false); },
  });
  const cancel = useMutation({ mutationFn: () => api.undecide(pr.ref, thread.id), onSuccess: ctx.setState });
  useEffect(() => setReply(d.reply), [d.reply]);
  useEffect(() => { if (d.reply && !draft.isPending) setEditingReason(false); }, [d.reply]); // eslint-disable-line react-hooks/exhaustive-deps

  const busy = drafting || draft.isPending;
  const cancelButton = (
    <button className="btn sm" onClick={() => cancel.mutate()} disabled={cancel.isPending || busy}>
      Cancel {decisionLabel(d.decision).toLowerCase()}
    </button>
  );
  const error = (draft.error || draftError || cancel.error) as Error | undefined;

  if (editingReason) {
    return (
      <div className={`composer ${compact ? "compact" : ""}`}>
      {compact && <div className="muted tiny">{reviewer}: {excerpt(thread.comments[0]?.body ?? "")}</div>}
        <label className="field">
          {prompt.label}
          <textarea className="input" rows={compact ? 2 : 3} value={reason} autoFocus={!compact}
            onChange={(e) => setReason(e.target.value)} placeholder={prompt.placeholder} />
        </label>
        <div className="row-wrap">
          <button className="btn primary sm" onClick={() => draft.mutate()} disabled={busy || (prompt.required && !reason.trim())}>
            {busy ? "Drafting…" : d.reply ? "Redraft reply" : "Draft reply"}
          </button>
          {d.reply && <button className="btn sm" onClick={() => { setReason(d.note); setEditingReason(false); }}>Keep current reply</button>}
          {cancelButton}
          {!compact && <span className="muted tiny">{aiName} turns this into a short reply to {reviewer}.</span>}
        </div>
        {error && <p className="error small">{error.message}</p>}
      </div>
    );
  }

  return (
    <div className={`composer ${compact ? "compact" : ""}`}>
      {compact && <div className="muted tiny">{reviewer}: {excerpt(thread.comments[0]?.body ?? "")}</div>}
      <label className="field">
        Reply to {reviewer} {busy && <span className="muted">· {aiName} is drafting…</span>}
        <textarea className="input" rows={compact ? 2 : 3} value={reply} disabled={busy} placeholder={busy ? "" : "No reply yet"}
          onChange={(e) => setReply(e.target.value)}
          onBlur={() => reply !== d.reply && api.patchDecision(pr.ref, thread.id, { reply }).then(ctx.setState)} />
      </label>
      <div className="muted tiny">
        {d.note ? <>Based on: “{d.note}” · </> : null}
        <button className="link tiny" onClick={() => setEditingReason(true)}>{d.decision === "push_back" ? (d.note ? "Edit your reason and redraft" : "Add your reason and redraft") : d.note ? "Edit and redraft" : "Add guidance and redraft"}</button>
        {" · "}Posted in Respond; you can still edit it there.
      </div>
      <div className="row-wrap">
        {cancelButton}
        {onNext && <button className="btn sm" onClick={onNext}>Next thread</button>}
      </div>
      {error && <p className="error small">{error.message}</p>}
    </div>
  );
}
