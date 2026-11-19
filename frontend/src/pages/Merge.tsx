import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useState } from "react";
import type { PRContext } from "../App";
import {
  api, type Conflict, type ConflictCategory, type ConflictOption, type MergeSession, type MergeView, type PRRef,
} from "../api";
import { CheckRow } from "../components/ChecksPanel";
import { DiffView } from "../components/Code";
import { LineDiffView, diffStats, lineDiff, plainLines, type DLine } from "../components/LineDiff";
import { prHref, useAIName } from "../lib";

// Update from main: Start → Triage → Resolve → Finish, like the comment workflow.

const CATEGORIES: ConflictCategory[] = ["auto", "input", "propose"];
const CATEGORY_LABEL: Record<ConflictCategory, string> = {
  auto: "Merge automatically",
  input: "Needs your input",
  propose: "Proposed code change",
};
const CATEGORY_SHORT: Record<ConflictCategory, string> = { auto: "Auto", input: "Input", propose: "Propose" };
const CATEGORY_HELP: Record<ConflictCategory, string> = {
  auto: "The right result is clear: both sides made the same change, one side only reformatted, or both added separate lines that belong together. Look them over, then accept them all at once.",
  input: "Both sides changed the same lines differently. Pick which version wins, keep both, or tell {ai} how to combine them.",
  propose: "Neither side alone is right, so {ai} wrote code that keeps both changes. Check it, edit it, or pick a side instead.",
};

export function mergeKey(ref: PRRef) {
  return ["merge", ref.owner, ref.repo, ref.number];
}

function optionLabel(o: ConflictOption, c: Conflict, base: string, ai: string): string {
  if (c.kind === "deleted_in_main") return o === "ours" ? "Keep the file, with this PR's changes" : `Delete it, as ${base} did`;
  if (c.kind === "deleted_in_pr") return o === "theirs" ? `Keep the file, with ${base}'s changes` : "Keep it deleted, as this PR did";
  return {
    ours: "Keep this PR's version",
    theirs: `Take ${base}'s version`,
    ours_then_theirs: "Keep both, this PR's first",
    theirs_then_ours: `Keep both, ${base}'s first`,
    proposal: `${ai}'s combined version`,
    custom: "Edit by hand",
    delete: "Delete the file",
  }[o];
}

function resultText(c: Conflict, o: ConflictOption | null, custom?: string | null): string | null {
  const join = (a: string | null, b: string | null) => {
    const x = a ?? "";
    const y = b ?? "";
    return x + (x && !x.endsWith("\n") && y ? "\n" : "") + y;
  };
  switch (o) {
    case "ours": return c.ours;
    case "theirs": return c.theirs;
    case "ours_then_theirs": return join(c.ours, c.theirs);
    case "theirs_then_ours": return join(c.theirs, c.ours);
    case "proposal": return c.proposal;
    case "custom": return custom ?? c.custom ?? c.ours;
    default: return null;
  }
}

const where = (c: Conflict) => `${c.path}${c.start_line && c.kind === "text" ? `:${c.start_line}` : ""}`;
const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

function ordered(s: MergeSession): Conflict[] {
  const rank = (c: Conflict) => (c.category ? CATEGORIES.indexOf(c.category) : 3);
  return s.files.flatMap((f) => f.conflict_ids.map((id) => s.conflicts[id])).sort((a, b) => rank(a) - rank(b));
}

export function Merge({ ctx, arg }: { ctx: PRContext; arg?: string }) {
  const { pr } = ctx.data!;
  const key = mergeKey(pr.ref);
  const q = useQuery({
    queryKey: key,
    queryFn: () => api.merge(pr.ref),
    refetchInterval: (query) => {
      const s = query.state.data?.session;
      return s && (s.status === "analyzing" || s.checks_running) ? 2000 : false;
    },
  });
  const [sub, id] = (arg ?? "").split("/");
  const { refetch } = q;
  useEffect(() => { refetch(); }, [sub, refetch]); // pick up changes made elsewhere (another window)
  if (q.error) return <main className="page"><div className="banner error">{(q.error as Error).message}</div></main>;
  if (!q.data) return <div className="pane-msg muted">Loading…</div>;
  const view = q.data;
  const s = view.session;
  if (!s) return <MergeStart prRef={pr.ref} branch={pr.head_ref} />;
  if (s.status === "committed") return <MergeDone prRef={pr.ref} s={s} />;
  const page = sub === "resolve" || sub === "finish" ? sub : "triage";
  return (
    <div className="merge-wrap">
      <MergeSteps prRef={pr.ref} s={s} page={page} />
      {page === "triage" && <ConflictTriage prRef={pr.ref} view={view} />}
      {page === "resolve" && <Resolve prRef={pr.ref} view={view} id={id ? decodeURIComponent(id) : undefined} />}
      {page === "finish" && <Finish prRef={pr.ref} view={view} />}
    </div>
  );
}

function useSetView(ref: PRRef) {
  const qc = useQueryClient();
  const key = mergeKey(ref);
  return (v: MergeView) => qc.setQueryData(key, v);
}

/** Sub-steps of the update, with progress counts. */
function MergeSteps({ prRef, s, page }: { prRef: PRRef; s: MergeSession; page: string }) {
  const all = Object.values(s.conflicts);
  const triaged = all.filter((c) => c.category).length;
  const resolved = all.filter((c) => c.resolved).length;
  const firstOpen = ordered(s).find((c) => !c.resolved);
  const steps = [
    { key: "triage", label: "Triage", count: `${triaged}/${all.length}`, href: prHref(prRef, "merge", "triage") },
    { key: "resolve", label: "Resolve", count: `${resolved}/${all.length}`, href: prHref(prRef, "merge", `resolve${firstOpen ? `/${encodeURIComponent(firstOpen.id)}` : ""}`) },
    { key: "finish", label: "Finish", count: "", href: prHref(prRef, "merge", "finish") },
  ];
  return (
    <div className="merge-steps">
      <span className="strong small">Update from {s.base_ref}</span>
      <nav className="steps" aria-label="Update steps">
        {steps.map((st, i) => (
          <Fragment key={st.key}>
            {i > 0 && <span className="step-sep" aria-hidden="true">→</span>}
            <a href={st.href} className={`step ${page === st.key ? "on" : ""}`} aria-current={page === st.key ? "step" : undefined}>
              {st.label}{st.count && ` · ${st.count}`}
            </a>
          </Fragment>
        ))}
      </nav>
      <span className="grow" />
      <DiscardButton prRef={prRef} />
    </div>
  );
}

function DiscardButton({ prRef }: { prRef: PRRef }) {
  const setView = useSetView(prRef);
  const discard = useMutation({ mutationFn: () => api.mergeDiscard(prRef), onSuccess: setView });
  return (
    <button className="link small" disabled={discard.isPending} onClick={() => {
      if (confirm("Discard this update? Your resolutions are thrown away. Your checkout was never changed, so nothing else is affected.")) discard.mutate();
    }}>
      Discard update
    </button>
  );
}

// Start

function MergeStart({ prRef, branch }: { prRef: PRRef; branch: string }) {
  const ai = useAIName();
  const setView = useSetView(prRef);
  const preview = useQuery({ queryKey: ["mergePreview", prRef.owner, prRef.repo, prRef.number], queryFn: () => api.mergePreview(prRef) });
  const start = useMutation({ mutationFn: () => api.mergeStart(prRef), onSuccess: setView });
  const p = preview.data;
  return (
    <main className="page narrow">
      <div className="page-head">
        <h1 className="grow">Update from {p?.base_ref ?? "main"}</h1>
      </div>
      {preview.error && <div className="banner error">{(preview.error as Error).message}</div>}
      {!p && !preview.error && <p className="muted">Fetching {branch} and main…</p>}
      {p?.fetch_error && <div className="banner warn">Couldn't fetch from GitHub, so this uses the last fetched copy: {p.fetch_error}</div>}
      {p && p.behind === 0 && (
        <section className="card"><p>This branch already has everything on {p.base_ref}. Nothing to update.</p></section>
      )}
      {p && p.behind > 0 && (
        <>
          <section className="card stack-gap">
            <h2>{p.base_ref} has {plural(p.behind, "commit")} that {branch} doesn't</h2>
            <p className="small">
              They change {plural(p.changed_files, "file")}.{" "}
              {p.conflicted_files.length === 0
                ? "None of them conflict with this PR: git can merge everything on its own. You'll still see what changes before anything is committed."
                : <>{plural(p.conflicted_files.length, "file")} will have conflicts:</>}
            </p>
            {p.conflicted_files.length > 0 && (
              <ul className="file-list">{p.conflicted_files.map((f) => <li key={f} className="mono small">{f}</li>)}</ul>
            )}
            <details>
              <summary className="small">Commits on {p.base_ref}</summary>
              <ul className="commits">
                {p.commits.map((c) => <li key={c.sha}><span className="mono">{c.sha}</span> {c.subject} <span className="muted">· {c.author}</span></li>)}
                {p.behind > p.commits.length && <li className="muted">… and {p.behind - p.commits.length} more</li>}
              </ul>
            </details>
            <div className="row-wrap">
              <button className="btn primary" onClick={() => start.mutate()} disabled={start.isPending}>
                {start.isPending ? "Merging…" : `Start update`}
              </button>
              {start.error && <span className="error small">{(start.error as Error).message}</span>}
            </div>
          </section>
          <section className="card stack-gap">
            <h2>How it works</h2>
            <ol className="how small">
              <li><strong>Merge in a copy.</strong> reviewr merges {p.base_ref} into a separate worktree at your checkout's current commit. Your files aren't touched until you commit at the end.</li>
              <li><strong>Triage.</strong> Simple conflicts are sorted by rule; {ai} sorts the rest into <em>merge automatically</em>, <em>needs your input</em> and <em>proposed code change</em>.</li>
              <li><strong>Resolve.</strong> For each conflict you see what this PR changed, what {p.base_ref} changed, and the code after the merge for each option.</li>
              <li><strong>Finish.</strong> Review each file before and after, run lint and tests on them, and commit the merge to {branch}. Pushing is a separate step.</li>
            </ol>
          </section>
        </>
      )}
    </main>
  );
}

// Triage

function ConflictTriage({ prRef, view }: { prRef: PRRef; view: MergeView }) {
  const s = view.session!;
  const ai = useAIName();
  const setView = useSetView(prRef);
  const all = ordered(s);
  const accept = useMutation({ mutationFn: (ids: string[]) => api.mergeAccept(prRef, ids), onSuccess: setView });
  const retry = useMutation({ mutationFn: () => api.mergeReanalyze(prRef), onSuccess: setView });
  const groups: { key: ConflictCategory | "none"; label: string; help: string; items: Conflict[] }[] = [
    ...CATEGORIES.map((k) => ({ key: k, label: CATEGORY_LABEL[k], help: CATEGORY_HELP[k].replace("{ai}", ai), items: all.filter((c) => c.category === k) })),
    { key: "none", label: "Not triaged yet", help: s.status === "analyzing" ? `${ai} is reading these.` : "Open each one to resolve it.", items: all.filter((c) => !c.category) },
  ];
  const open = all.filter((c) => !c.resolved);
  const firstOpen = open[0];

  return (
    <main className="page">
      <section className="card stack-gap">
        <p>
          Merging {plural(s.commit_count, "commit")} from <span className="mono">{s.base_ref}</span> into{" "}
          <span className="mono">{s.head_ref}</span>. {plural(s.clean_files.length, "file")} merged without conflicts;{" "}
          {all.length === 0 ? "nothing conflicts." : `${plural(all.length, "conflict")} in ${plural(s.files.length, "file")}.`}
        </p>
        {s.status === "analyzing" && <div className="banner">{ai} is triaging {plural(all.filter((c) => !c.category).length, "conflict")}… The rest are ready below.</div>}
        {s.error && (
          <div className="banner error row-wrap">
            <span className="grow">{s.error}</span>
            <button className="btn sm" onClick={() => retry.mutate()} disabled={retry.isPending}>Try again</button>
          </div>
        )}
      </section>

      {groups.map((g) => {
        if (!g.items.length) return null;
        const pending = g.items.filter((c) => !c.resolved && c.recommended);
        return (
          <section key={g.key} className={`card flush conflict-group cat-${g.key}`}>
            <div className="list-head">
              <h2 className="grow">{g.label} · {g.items.length}</h2>
              {g.key === "auto" && pending.length > 0 && (
                <button className="btn sm primary" onClick={() => accept.mutate(pending.map((c) => c.id))} disabled={accept.isPending}>
                  Accept all {pending.length}
                </button>
              )}
            </div>
            <p className="muted small pad-x">{g.help}</p>
            {g.items.map((c) => <TriageRow key={c.id} prRef={prRef} c={c} base={s.base_ref} ai={ai} />)}
          </section>
        );
      })}
      {accept.error && <p className="error small">{(accept.error as Error).message}</p>}

      <div className="row-wrap">
        <span className="grow" />
        {firstOpen ? (
          <a className="btn primary" href={prHref(prRef, "merge", `resolve/${encodeURIComponent(firstOpen.id)}`)}>
            Next: Resolve {plural(open.length, "conflict")} <span aria-hidden="true">→</span>
          </a>
        ) : (
          <a className="btn primary" href={prHref(prRef, "merge", "finish")}>Next: Finish <span aria-hidden="true">→</span></a>
        )}
      </div>
    </main>
  );
}

function TriageRow({ prRef, c, base, ai }: { prRef: PRRef; c: Conflict; base: string; ai: string }) {
  const setView = useSetView(prRef);
  const [preview, setPreview] = useState(false);
  const recat = useMutation({ mutationFn: (category: ConflictCategory) => api.mergePatch(prRef, c.id, { category }), onSuccess: setView });
  const shown = c.choice ?? c.recommended;
  return (
    <div className="list-row-wrap">
      <div className="list-row">
        <span className="mark" aria-hidden="true">{c.resolved ? "✓" : ""}</span>
        <span className="grow min0">
          <a href={prHref(prRef, "merge", `resolve/${encodeURIComponent(c.id)}`)} className="block truncate">{c.title || "Conflict"}</a>
          <span className="mono muted tiny">{where(c)}</span>
          {shown && (
            <span className="muted tiny"> · {c.resolved ? "Resolved: " : "Suggested: "}{optionLabel(shown, c, base, c.ai_name ?? ai)}</span>
          )}
        </span>
        {shown && c.kind === "text" && (
          <button className="link tiny" onClick={() => setPreview(!preview)}>{preview ? "Hide result" : "Show result"}</button>
        )}
        <div className="dec" role="group" aria-label="Category">
          {CATEGORIES.map((k) => (
            <button key={k} className={c.category === k ? "on" : ""} title={CATEGORY_LABEL[k]} onClick={() => recat.mutate(k)} disabled={recat.isPending}>
              {CATEGORY_SHORT[k]}
            </button>
          ))}
        </div>
      </div>
      {preview && shown && (
        <div className="reply-inline">
          <p className="muted tiny">{c.reason}</p>
          <div className="label">After the merge, compared with this PR</div>
          <LineDiffView lines={withContext(c, lineDiff(c.ours, resultText(c, shown)))} />
        </div>
      )}
    </div>
  );
}

function withContext(c: Conflict, lines: DLine[]): DLine[] {
  return [...plainLines(c.before, "ctx"), ...lines, ...plainLines(c.after, "ctx")];
}

// Resolve

function Resolve({ prRef, view, id }: { prRef: PRRef; view: MergeView; id?: string }) {
  const s = view.session!;
  const list = ordered(s);
  const selected = list.find((c) => c.id === id) ?? list.find((c) => !c.resolved) ?? list[0];
  const go = (c?: Conflict) => { if (c) location.hash = prHref(prRef, "merge", `resolve/${encodeURIComponent(c.id)}`); };
  const nextOpen = (after: Conflict) => {
    const i = list.findIndex((c) => c.id === after.id);
    return [...list.slice(i + 1), ...list.slice(0, i)].find((c) => !c.resolved && c.id !== after.id);
  };
  if (!selected) {
    return <main className="page"><p className="muted">No conflicts. <a href={prHref(prRef, "merge", "finish")}>Go to Finish</a>.</p></main>;
  }
  return (
    <div className="triage">
      <aside className="queue" aria-label="Conflicts">
        {[...CATEGORIES, null].map((k) => {
          const rows = list.filter((c) => c.category === k);
          if (!rows.length) return null;
          return (
            <div key={k ?? "none"}>
              <div className="queue-group"><span className={`cat-dot cat-${k ?? "none"}`} />{k ? CATEGORY_LABEL[k] : "Not triaged yet"} · {rows.length}</div>
              {rows.map((c) => (
                <button key={c.id} className={`qrow ${c.id === selected.id ? "sel" : ""}`} onClick={() => go(c)}>
                  <span className="mark" aria-hidden="true">{c.resolved ? "✓" : ""}</span>
                  <span className="grow min0">
                    <span className="truncate block">{c.title || "Conflict"}</span>
                    <span className="mono muted tiny">{where(c)}</span>
                  </span>
                </button>
              ))}
            </div>
          );
        })}
      </aside>
      <main className="triage-main">
        <ConflictCard
          key={selected.id} prRef={prRef} s={s} c={selected} options={view.options[selected.id] ?? []}
          onNav={(dir) => go(list[list.findIndex((c) => c.id === selected.id) + dir])}
          onDone={() => {
            const n = nextOpen(selected);
            if (n) go(n);
            else location.hash = prHref(prRef, "merge", "finish");
          }}
        />
      </main>
    </div>
  );
}

type Compare = "ours" | "theirs" | "none";

function ConflictCard({ prRef, s, c, options, onNav, onDone }: {
  prRef: PRRef; s: MergeSession; c: Conflict; options: ConflictOption[]; onNav: (dir: 1 | -1) => void; onDone: () => void;
}) {
  const current = useAIName();
  const ai = c.ai_name ?? current; // the AI that wrote this conflict's suggestion
  const setView = useSetView(prRef);
  const base = s.base_ref;
  const file = s.files.find((f) => f.path === c.path)!;
  useEffect(() => { window.scrollTo(0, 0); }, []); // the card is keyed by conflict, so this runs per conflict
  const initial: ConflictOption = c.choice ?? c.recommended ?? options[0];
  const [pick, setPick] = useState<ConflictOption>(initial);
  const [custom, setCustom] = useState<string>(c.custom ?? resultText(c, c.recommended ?? "ours") ?? "");
  const [compare, setCompare] = useState<Compare>(c.kind === "deleted_in_pr" ? "theirs" : "ours");
  const [instruction, setInstruction] = useState("");

  const save = useMutation({
    mutationFn: () => api.mergePatch(prRef, c.id, pick === "custom" ? { choice: pick, custom } : { choice: pick }),
    onSuccess: (v) => { setView(v); onDone(); },
  });
  const reopen = useMutation({ mutationFn: () => api.mergePatch(prRef, c.id, { resolved: false }), onSuccess: setView });
  const propose = useMutation({
    mutationFn: () => api.mergePropose(prRef, c.id, instruction),
    onSuccess: (v) => { setView(v); setPick("proposal"); },
  });

  // Keyboard: 1–9 pick an option, Enter accepts, j/k move between conflicts.
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.tagName === "SELECT" || e.metaKey || e.ctrlKey || e.altKey) return;
      const n = Number(e.key);
      if (n >= 1 && n <= options.length) setPick(options[n - 1]);
      else if (e.key === "Enter" && !save.isPending) save.mutate();
      else if (e.key === "j") onNav(1);
      else if (e.key === "k") onNav(-1);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  });

  const result = pick === "delete" ? null : resultText(c, pick, custom);
  const against = compare === "ours" ? c.ours : compare === "theirs" ? c.theirs : null;
  const resultLines = compare === "none" ? plainLines(result) : lineDiff(against, result);
  const stats = diffStats(resultLines);
  const fileLevel = c.kind !== "text";
  const nInFile = file.conflict_ids.length;
  const unchanged = c.resolved && pick === c.choice && (pick !== "custom" || custom === c.custom);

  return (
    <div className="triage-card-wrap">
      <article className="card stack-gap">
        <div className="row-wrap">
          <span className="mono small">{where(c)}</span>
          {nInFile > 1 && <span className="muted tiny">conflict {c.index + 1} of {nInFile} in this file</span>}
          {c.category && <span className={`pill cat-pill cat-${c.category}`}>{CATEGORY_LABEL[c.category]}</span>}
          {c.resolved && <span className="pill ok">Resolved</span>}
          <span className="grow" />
          <button className="btn sm" onClick={() => onNav(-1)} title="Previous (k)">←</button>
          <button className="btn sm" onClick={() => onNav(1)} title="Next (j)">→</button>
        </div>
        <h1 className="h-thread">{c.title || `Conflict in ${c.path}`}</h1>

        {c.reason && (
          <div className="suggest">
            <strong className="small">
              {c.source === "rule" ? "Rule" : ai} suggests{c.recommended ? `: ${optionLabel(c.recommended, c, base, ai)}` : ""}
            </strong>
            <p className="small">{c.reason}</p>
          </div>
        )}

        <div>
          <h2 className="h3">What each side changed</h2>
          <p className="muted tiny">Each side compared with the code they both started from.</p>
        </div>
        <div className="two-col">
          <SideDiff title="This PR" sub={s.head_ref} c={c} text={c.ours} commits={file.pr_commits} deletedNote="This PR deleted the file." />
          <SideDiff title={base} sub="the base branch" c={c} text={c.theirs} commits={file.main_commits} deletedNote={`${base} deleted the file.`} />
        </div>

        <div>
          <h2 className="h3">Choose the result</h2>
        </div>
        <div className="opt-list" role="radiogroup" aria-label="Resolution">
          {options.map((o, i) => (
            <label key={o} className={`opt ${pick === o ? "on" : ""}`}>
              <input type="radio" name={`opt-${c.id}`} checked={pick === o} onChange={() => setPick(o)} />
              <span className="kbd">{i + 1}</span>
              <span className="grow">{optionLabel(o, c, base, ai)}</span>
              {o === c.recommended && <span className="pill">Suggested</span>}
              {o === c.choice && c.resolved && <span className="pill ok">Chosen</span>}
            </label>
          ))}
        </div>
        {pick === "custom" && (
          <textarea className="input mono code-edit" rows={Math.min(24, Math.max(6, custom.split("\n").length + 1))}
            value={custom} onChange={(e) => setCustom(e.target.value)} aria-label="Your version of these lines" />
        )}
        {!fileLevel && (
          <div className="ask-ai">
            <label className="small strong" htmlFor={`ins-${c.id}`}>Tell {current} how to combine them</label>
            <div className="row-wrap">
              <input id={`ins-${c.id}`} className="input grow" value={instruction} onChange={(e) => setInstruction(e.target.value)}
                placeholder={`Optional, e.g. "keep ${base}'s new argument, and this PR's logging"`} />
              <button className="btn sm" onClick={() => propose.mutate()} disabled={propose.isPending}>
                {propose.isPending ? "Writing…" : c.proposal ? `Ask ${current} again` : `Ask ${current}`}
              </button>
            </div>
            {propose.error && <p className="error small">{(propose.error as Error).message}</p>}
          </div>
        )}

        <div className="row-between">
          <div>
            <h2 className="h3">After the merge</h2>
            {!fileLevel && result !== null && compare !== "none" && (
              <p className="muted tiny">
                {stats.added || stats.removed
                  ? `${plural(stats.added, "line")} added, ${stats.removed} removed compared with ${compare === "ours" ? "this PR" : base}. Dimmed lines are unchanged code around the conflict.`
                  : `Same as ${compare === "ours" ? "this PR" : base}.`}
              </p>
            )}
          </div>
          {!fileLevel && (
            <div className="seg" role="group" aria-label="Compare with">
              <button className={compare === "ours" ? "on" : ""} onClick={() => setCompare("ours")}>vs this PR</button>
              <button className={compare === "theirs" ? "on" : ""} onClick={() => setCompare("theirs")}>vs {base}</button>
              <button className={compare === "none" ? "on" : ""} onClick={() => setCompare("none")}>Plain</button>
            </div>
          )}
        </div>
        {c.kind === "binary" ? (
          <p className="small">The {pick === "ours" ? "PR's" : `${base}'s`} version of the file is kept as it is.</p>
        ) : result === null ? (
          <p className="small strong">The file is deleted.</p>
        ) : fileLevel ? (
          <LineDiffView lines={lineDiff(c.ours ?? "", result)} empty="(empty file)" fold={4} />
        ) : (
          <LineDiffView lines={withContext(c, resultLines)} empty="(the conflicting lines are removed)" />
        )}
        {c.kind === "lockfile" && (
          <p className="muted small">
            After committing the merge, run your package manager in your checkout to add this PR's dependency changes back, and
            commit the regenerated lockfile.
          </p>
        )}

        <div className="row-wrap">
          <button className="btn primary" onClick={() => save.mutate()} disabled={save.isPending || unchanged}>
            {c.resolved ? (unchanged ? "Resolved" : "Update resolution") : "Accept"} <span className="kbd">↵</span>
          </button>
          {c.resolved && <button className="link small" onClick={() => reopen.mutate()} disabled={reopen.isPending}>Mark unresolved</button>}
          {save.error && <span className="error small">{(save.error as Error).message}</span>}
        </div>
      </article>
      <aside className="stack-gap">
        <div className="card">
          <div className="label">Keys</div>
          <div className="muted small keys">
            <div><span className="kbd">1</span>–<span className="kbd">{options.length}</span> choose an option</div>
            <div><span className="kbd">↵</span> accept and go to the next</div>
            <div><span className="kbd">j</span> / <span className="kbd">k</span> next / previous</div>
          </div>
        </div>
      </aside>
    </div>
  );
}

function SideDiff({ title, sub, c, text, commits, deletedNote }: {
  title: string; sub: string; c: Conflict; text: string | null; commits: { sha: string; subject: string }[]; deletedNote: string;
}) {
  return (
    <div className="stack-gap side">
      <div><span className="strong small">{title}</span> <span className="muted tiny mono">{sub}</span></div>
      {c.kind === "binary" ? (
        <p className="muted small">Binary file, changed on this side.</p>
      ) : text === null ? (
        <p className="small strong">{deletedNote}</p>
      ) : (
        <LineDiffView lines={lineDiff(c.base, text)} empty="(removed these lines)" fold={c.kind === "text" ? 8 : 3} />
      )}
      {commits.length > 0 && (
        <ul className="commits tiny">
          {commits.slice(0, 3).map((x) => <li key={x.sha}><span className="mono">{x.sha}</span> {x.subject}</li>)}
        </ul>
      )}
    </div>
  );
}

// Finish

function Finish({ prRef, view }: { prRef: PRRef; view: MergeView }) {
  const s = view.session!;
  const setView = useSetView(prRef);
  const all = ordered(s);
  const open = all.filter((c) => !c.resolved);
  const [message, setMessage] = useState(`Merge ${s.base_ref} into ${s.head_ref}`);
  const checks = useMutation({ mutationFn: () => api.mergeChecks(prRef), onSuccess: setView });
  const qc = useQueryClient();
  const commit = useMutation({
    mutationFn: () => api.mergeCommit(prRef, message),
    onSuccess: (v) => {
      setView(v);
      // The branch moved: refresh "behind main" in the header and Overview, and the workspace status.
      qc.invalidateQueries({ queryKey: ["mergePreview", prRef.owner, prRef.repo, prRef.number] });
      qc.invalidateQueries({ queryKey: ["work", prRef.number] });
    },
  });
  const report = s.check_report;
  const busy = s.checks_running || checks.isPending;

  return (
    <main className="page">
      {open.length > 0 && (
        <div className="banner warn row-wrap">
          <span className="grow">{plural(open.length, "conflict")} still need a resolution before the merge can be committed.</span>
          <a className="btn sm" href={prHref(prRef, "merge", `resolve/${encodeURIComponent(open[0].id)}`)}>Resolve them</a>
        </div>
      )}

      <section className="card stack-gap">
        <h2>What changes on {s.head_ref}</h2>
        <p className="muted small">
          Each file that had conflicts, as it is on your branch now compared with after the merge. Red lines are removed from
          your branch, green lines are added. This includes {s.base_ref}'s changes that merged without conflicts in the same files.
        </p>
        {s.files.map((f) => <FileDiff key={f.path} prRef={prRef} s={s} path={f.path} />)}
        <details>
          <summary className="small">{plural(s.clean_files.length, "other file")} from {s.base_ref} merged without conflicts</summary>
          <ul className="file-list">{s.clean_files.map((p) => <li key={p} className="mono small">{p}</li>)}</ul>
        </details>
        <details>
          <summary className="small">{plural(s.commit_count, "commit")} from {s.base_ref}</summary>
          <ul className="commits">{s.commits.map((c) => <li key={c.sha}><span className="mono">{c.sha}</span> {c.subject} <span className="muted">· {c.author}</span></li>)}</ul>
        </details>
      </section>

      <section className="card stack-gap">
        <div className="row-between">
          <h2>Checks</h2>
          <button className="btn sm" onClick={() => checks.mutate()} disabled={busy || open.length > 0}>
            {busy ? "Running…" : report ? "Run again" : "Run checks"}
          </button>
        </div>
        <p className="muted small">
          Lint and tests for the files that had conflicts, run on the merged code with this branch's dependencies. Optional, but
          they catch a resolution that doesn't compile or breaks a test.
        </p>
        {busy && (
          <p className="small">
            Running. If the merge changes dependency files (lockfiles, pyproject.toml, package.json), the merged code's dependencies
            are installed first, which can take several minutes. The install is reused afterwards.
          </p>
        )}
        {checks.error && <p className="error small">{(checks.error as Error).message}</p>}
        {report?.runs.map((r) => <CheckRow key={r.name} run={r} />)}
      </section>

      <section className="card stack-gap">
        <h2>Commit the merge</h2>
        <p className="muted small">
          Creates the merge commit and moves your checkout of <span className="mono">{s.head_ref}</span> to it. Your uncommitted
          edits stay as they are; if any of them are in files the merge changes, nothing is moved and you'll be told which. Nothing is
          pushed yet.
        </p>
        <input className="input" value={message} onChange={(e) => setMessage(e.target.value)} aria-label="Commit message" />
        <div className="row-wrap">
          <button className="btn primary" onClick={() => commit.mutate()} disabled={commit.isPending || open.length > 0 || busy}>
            {commit.isPending ? "Committing…" : `Commit merge to ${s.head_ref}`}
          </button>
          {report && report.runs.some((r) => r.status === "failed" || r.status === "error") && (
            <span className="error small">Some checks failed. You can still commit and fix them afterwards.</span>
          )}
        </div>
        {commit.error && <pre className="code error-box"><div>{(commit.error as Error).message}</div></pre>}
      </section>
    </main>
  );
}

function FileDiff({ prRef, s, path }: { prRef: PRRef; s: MergeSession; path: string }) {
  const [open, setOpen] = useState(false);
  const f = s.files.find((x) => x.path === path)!;
  const ids = f.conflict_ids;
  const done = ids.filter((id) => s.conflicts[id].resolved).length;
  const stamp = useMemo(() => ids.map((id) => `${s.conflicts[id].choice}:${s.conflicts[id].custom?.length ?? 0}`).join(","), [ids, s]);
  const diff = useQuery({
    queryKey: ["mergeDiff", prRef.number, path, stamp],
    queryFn: () => api.mergeDiff(prRef, path),
    enabled: open && done === ids.length,
  });
  return (
    <details className="file-diff" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="row-wrap">
        <span className="mono small grow">{path}</span>
        <span className={`pill ${done === ids.length ? "ok" : "warn"}`}>{done}/{ids.length} resolved</span>
      </summary>
      {done < ids.length ? (
        <p className="muted small">Resolve every conflict in this file to see it after the merge.</p>
      ) : diff.data ? (
        diff.data.diff ? <DiffView text={diff.data.diff} maxLines={800} /> : <p className="muted small">No change to this file on your branch.</p>
      ) : (
        <p className="muted small">Loading…</p>
      )}
    </details>
  );
}

// Done

function MergeDone({ prRef, s }: { prRef: PRRef; s: MergeSession }) {
  const setView = useSetView(prRef);
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const push = useMutation({
    mutationFn: () => api.mergePush(prRef),
    onSuccess: (v) => { setView(v); setConfirming(false); qc.invalidateQueries({ queryKey: ["work", prRef.number] }); },
  });
  const again = useMutation({ mutationFn: () => api.mergeDiscard(prRef), onSuccess: setView });
  return (
    <main className="page narrow">
      <section className="card stack-gap">
        <h1>Merged {s.base_ref} into {s.head_ref}</h1>
        <p className="small">
          Commit <span className="mono">{s.commit_sha?.slice(0, 10)}</span> is on your checkout of {s.head_ref}, with{" "}
          {plural(Object.keys(s.conflicts).length, "conflict")} resolved and {plural(s.commit_count, "commit")} from {s.base_ref}.
        </p>
        {s.pushed ? (
          <p className="small"><span className="pill ok">Pushed</span> {s.head_ref} is on GitHub with the merge.</p>
        ) : (
          <>
            <p className="muted small">
              It isn't on GitHub yet. Push it now, or leave it: Respond pushes the branch together with your replies.
            </p>
            {!confirming ? (
              <div className="row-wrap"><button className="btn" onClick={() => setConfirming(true)}>Push to GitHub…</button></div>
            ) : (
              <div className="info-bar">
                <span className="grow small">Push {s.head_ref} to origin? Reviewers will see the merge commit. This only succeeds if nobody else pushed to the branch meanwhile.</span>
                <button className="btn primary sm" onClick={() => push.mutate()} disabled={push.isPending}>{push.isPending ? "Pushing…" : "Yes, push"}</button>
                <button className="btn sm" onClick={() => setConfirming(false)}>Cancel</button>
              </div>
            )}
            {push.error && <p className="error small">{(push.error as Error).message}</p>}
          </>
        )}
        <div className="row-wrap">
          <a className="btn" href={prHref(prRef)}>Back to Overview</a>
          <button className="link small" onClick={() => again.mutate()} disabled={again.isPending}>Check {s.base_ref} for newer commits</button>
        </div>
      </section>
    </main>
  );
}

/** Branch status for the header and the Overview: behind main, conflicts, or an update in progress. */
export function useBranchStatus(ref: PRRef) {
  const merge = useQuery({ queryKey: mergeKey(ref), queryFn: () => api.merge(ref), retry: false });
  const preview = useQuery({
    queryKey: ["mergePreview", ref.owner, ref.repo, ref.number],
    queryFn: () => api.mergePreview(ref),
    retry: false,
    staleTime: 5 * 60_000,
    enabled: merge.isSuccess && (!merge.data.session || merge.data.session.status === "committed"),
  });
  return { session: merge.data?.session ?? null, preview: preview.data, error: preview.error as Error | null };
}

export function MergeBadge({ prRef }: { prRef: PRRef }) {
  const { session, preview } = useBranchStatus(prRef);
  if (session && session.status !== "committed") {
    const all = Object.values(session.conflicts);
    const done = all.filter((c) => c.resolved).length;
    return <a className="merge-badge on" href={prHref(prRef, "merge")}>Updating from {session.base_ref} · {done}/{all.length} resolved</a>;
  }
  if (!preview || preview.behind === 0) return null;
  const n = preview.conflicted_files.length;
  return (
    <a className={`merge-badge ${n ? "warn" : ""}`} href={prHref(prRef, "merge")} title={`Update this branch from ${preview.base_ref}`}>
      {preview.base_ref} +{preview.behind}{n ? ` · ${plural(n, "conflict")}` : ""}
    </a>
  );
}

export function BranchStatusCard({ prRef }: { prRef: PRRef }) {
  const { session, preview, error } = useBranchStatus(prRef);
  if (error) return null;
  const inProgress = session && session.status !== "committed";
  if (!inProgress && !preview) return null;
  return (
    <section className="card stack-gap">
      <h2>Branch vs {inProgress ? session.base_ref : preview!.base_ref}</h2>
      {inProgress ? (
        <>
          <p className="small">An update from {session.base_ref} is in progress: {Object.values(session.conflicts).filter((c) => c.resolved).length} of {plural(Object.keys(session.conflicts).length, "conflict")} resolved.</p>
          <a className="btn sm" href={prHref(prRef, "merge")}>Continue the update</a>
        </>
      ) : preview!.behind === 0 ? (
        <p className="muted small">Up to date with {preview!.base_ref}.</p>
      ) : (
        <>
          <p className="small">
            {preview!.base_ref} has {plural(preview!.behind, "commit")} this branch doesn't.{" "}
            {preview!.conflicted_files.length ? `${plural(preview!.conflicted_files.length, "file")} would conflict.` : "They merge without conflicts."}
          </p>
          <a className="btn sm" href={prHref(prRef, "merge")}>Update from {preview!.base_ref}</a>
        </>
      )}
    </section>
  );
}
