import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import type { PRContext } from "../App";
import { api, editsConflict, type CICategory, type CIFailure, type CISession, type PRRef } from "../api";
import { CheckoutPath } from "../components/CheckoutPath";
import { ResultCard, useCheckoutDetails } from "../components/ResultCard";
import { BrandIcon, GitHubLink } from "../components/Brand";
import { Inline } from "../components/Inline";
import { KeyHelp } from "../components/KeyHelp";
import { ProposedFix } from "../components/ProposedFix";
import { Spinner } from "../components/Spinner";
import { prHref, useAIName } from "../lib";
import { GitHubCommits, type WorkflowStep } from "./Merge";
import { EditConflicts, EditsConflictPrompt } from "../components/EditConflicts";

// Fix CI: Start → Triage → Fix → Finish, like the other two workflows.

const CATEGORIES: CICategory[] = ["fix", "rerun", "unrelated"];
const CATEGORY_LABEL: Record<CICategory, string> = {
  fix: "Fix in this PR",
  rerun: "Re-run",
  unrelated: "Not this PR",
};
const CATEGORY_HELP: Record<CICategory, string> = {
  fix: "The PR's changes caused these, or they need a change on this branch. {ai} can propose a fix; you see the diff before it's committed.",
  rerun: "Flaky or infrastructure: timeouts, cancelled jobs, network or runner problems. Re-running the job on GitHub is the likely fix.",
  unrelated: "Not caused by this PR, and re-running won't help, for example the base branch is broken. Dismiss them, or fix them anyway.",
};
const STATUS_LABEL: Record<CIFailure["status"], string> = { open: "", fixed: "Fixed", rerun: "Re-run requested", dismissed: "Dismissed" };

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
export const ciKey = (ref: PRRef) => ["ci", ref.owner, ref.repo, ref.number];
export const ciStatusKey = (ref: PRRef) => ["ciStatus", ref.owner, ref.repo, ref.number];

function ordered(s: CISession): CIFailure[] {
  const rank = (f: CIFailure) => (f.category ? CATEGORIES.indexOf(f.category) : 3);
  return s.order.map((id) => s.failures[id]).filter(Boolean).sort((a, b) => rank(a) - rank(b));
}
const done = (f: CIFailure) => f.status !== "open";
/** The agent is working on this failure: fixing it, or the whole set is being analyzed. */
const busy = (f: CIFailure, s: CISession) => f.fix?.status === "running" || (s.status === "analyzing" && !f.category);

function useSetSession(ref: PRRef) {
  const qc = useQueryClient();
  return (v: { session: CISession | null }) => qc.setQueryData(ciKey(ref), v);
}

export function CI({ ctx, arg }: { ctx: PRContext; arg?: string }) {
  const { pr } = ctx.data!;
  const q = useQuery({
    queryKey: ciKey(pr.ref),
    queryFn: () => api.ci(pr.ref),
    refetchInterval: (query) => {
      const s = query.state.data?.session;
      return s && (s.status === "analyzing" || Object.values(s.failures).some((f) => f.fix?.status === "running")) ? 2000 : false;
    },
  });
  const [sub, id] = (arg ?? "").split("/");
  const { refetch } = q;
  useEffect(() => { refetch(); }, [sub, refetch]);
  if (q.error) return <main className="page"><div className="banner error">{(q.error as Error).message}</div></main>;
  if (!q.data) return <div className="pane-msg muted">Loading…</div>;
  const s = q.data.session;
  if (!s) return <CIStart prRef={pr.ref} />;
  if (sub === "fix") return <CIFix prRef={pr.ref} s={s} id={id ? decodeURIComponent(id) : undefined} />;
  if (sub === "finish") return <CIFinish prRef={pr.ref} s={s} branch={pr.head_ref} />;
  return <CITriage prRef={pr.ref} s={s} />;
}

/** The steps of the CI workflow, for the header's tracker. */
export function ciSteps(prRef: PRRef, s: CISession | null, arg?: string): WorkflowStep[] {
  const sub = (arg ?? "").split("/")[0];
  if (!s) {
    return [
      { key: "start", label: "Start", href: prHref(prRef, "ci"), on: true },
      { key: "triage", label: "Triage", on: false },
      { key: "fix", label: "Fix", on: false },
      { key: "finish", label: "Finish", on: false },
    ];
  }
  const all = Object.values(s.failures);
  const firstOpen = ordered(s).find((f) => !done(f));
  const page = sub === "fix" || sub === "finish" ? sub : "triage";
  return [
    { key: "triage", label: `Triage · ${all.filter((f) => f.category).length}/${all.length}`, href: prHref(prRef, "ci", "triage"), on: page === "triage" },
    { key: "fix", label: `Fix · ${all.filter(done).length}/${all.length}`, on: page === "fix",
      href: prHref(prRef, "ci", `fix${firstOpen ? `/${encodeURIComponent(firstOpen.id)}` : ""}`) },
    { key: "finish", label: "Finish", href: prHref(prRef, "ci", "finish"), on: page === "finish" },
  ];
}

/** For the workflow switch: "3 failing", or progress during a review. */
export function useCIStatus(ref: PRRef) {
  const session = useQuery({ queryKey: ciKey(ref), queryFn: () => api.ci(ref), retry: false });
  const status = useQuery({ queryKey: ciStatusKey(ref), queryFn: () => api.ciStatus(ref), retry: false, staleTime: 60_000 });
  const s = session.data?.session ?? null;
  const failing = status.data?.failures.length ?? 0;
  const chip = s
    ? `${Object.values(s.failures).filter(done).length}/${Object.keys(s.failures).length}`
    : failing ? `${failing} failing` : null;
  return { session: s, status: status.data, show: !!s || failing > 0, chip, warn: !s && failing > 0 };
}

// Start

function CIStart({ prRef }: { prRef: PRRef }) {
  const ai = useAIName();
  const setSession = useSetSession(prRef);
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ciStatusKey(prRef), queryFn: () => api.ciStatus(prRef, true) });
  const start = useMutation({ mutationFn: () => api.ciStart(prRef), onSuccess: setSession });
  const st = status.data;
  return (
    <main className="page narrow">
      <div className="page-head">
        <h1 className="grow">Fix CI</h1>
        <button className="btn sm" onClick={() => { qc.removeQueries({ queryKey: ciStatusKey(prRef) }); status.refetch(); }} disabled={status.isFetching}>
          {status.isFetching ? "Checking…" : "Check again"}
        </button>
      </div>
      {status.error && <div className="banner error">{(status.error as Error).message}</div>}
      {st && st.failures.length === 0 && <section className="card"><p>No checks are failing on this PR{st.state ? ` (CI is ${st.state.toLowerCase()})` : ""}.</p></section>}
      {st && st.failures.length > 0 && (
        <>
          <section className="card stack-gap">
            <h2>{plural(st.failures.length, "check")} failing on the latest commit</h2>
            <ul className="file-list">
              {st.failures.map((f) => (
                <li key={f.id} className="small">
                  <strong>{f.name}</strong>{f.workflow && f.workflow !== f.name ? <span className="muted"> · {f.workflow}</span> : null}
                  <span className="muted"> · {f.conclusion.toLowerCase()}</span>{f.required && <span className="pill warn">Required</span>}
                  {f.url && <> · <GitHubLink href={f.url}>logs on GitHub</GitHubLink></>}
                </li>
              ))}
            </ul>
            <div className="row-wrap">
              <button className="btn primary" onClick={() => start.mutate()} disabled={start.isPending}>
                {start.isPending ? "Starting…" : "Analyze failures"}
              </button>
              {start.error && <span className="error small">{(start.error as Error).message}</span>}
            </div>
          </section>
          <section className="card stack-gap">
            <h2>How it works</h2>
            <ol className="how small">
              <li><strong>Analyze.</strong> reviewr downloads the log of each failing job and keeps the step that failed. {ai} reads it with GitHub's annotations.</li>
              <li><strong>Triage.</strong> Each failure goes into <em>fix in this PR</em>, <em>re-run</em> (flaky or infrastructure), or <em>not this PR</em>. Change any you disagree with.</li>
              <li><strong>Fix.</strong> For each failure: ask {ai} for a fix and review the diff before it's committed, re-run the job on GitHub, mark it fixed yourself, or dismiss it.</li>
              <li><strong>Finish.</strong> Push the fixes; CI runs again on GitHub. Nothing is pushed or re-run without your confirmation.</li>
            </ol>
          </section>
        </>
      )}
    </main>
  );
}

// Triage

function CITriage({ prRef, s }: { prRef: PRRef; s: CISession }) {
  const ai = useAIName();
  const setSession = useSetSession(prRef);
  const retry = useMutation({ mutationFn: () => api.ciReanalyze(prRef), onSuccess: setSession });
  const all = ordered(s);
  const open = all.filter((f) => !done(f));
  const groups = [
    ...CATEGORIES.map((k) => ({ key: k as string, label: CATEGORY_LABEL[k], help: CATEGORY_HELP[k].replace("{ai}", ai), items: all.filter((f) => f.category === k) })),
    { key: "none", label: "Not triaged yet", help: s.status === "analyzing" ? `${ai} is reading the logs.` : "Open each one to decide.", items: all.filter((f) => !f.category) },
  ];
  return (
    <main className="page">
      <section className="card stack-gap">
        <div className="row-between">
          <p>{plural(all.length, "failing check")} on <span className="mono">{s.head_sha.slice(0, 10)}</span>, the PR's latest commit on GitHub.</p>
          <DiscardCI prRef={prRef} />
        </div>
        {s.status === "analyzing" && <div className="banner">Downloading logs and asking {ai} to triage the failures…</div>}
        {s.error && (
          <div className="banner error row-wrap">
            <span className="grow">{s.error}</span>
            <button className="btn sm" onClick={() => retry.mutate()} disabled={retry.isPending}>Try again</button>
          </div>
        )}
      </section>
      {groups.map((g) => g.items.length > 0 && (
        <section key={g.key} className={`card flush conflict-group ci-${g.key}`}>
          <div className="list-head"><h2 className="grow">{g.label} · {g.items.length}</h2></div>
          <p className="muted small pad-x">{g.help}</p>
          {g.items.map((f) => <TriageRow key={f.id} prRef={prRef} f={f} s={s} />)}
        </section>
      ))}
      <div className="row-wrap">
        <span className="grow" />
        {open.length ? (
          <a className="btn primary" href={prHref(prRef, "ci", `fix/${encodeURIComponent(open[0].id)}`)}>Next: Fix {plural(open.length, "failure")} <span aria-hidden="true">→</span></a>
        ) : (
          <a className="btn primary" href={prHref(prRef, "ci", "finish")}>Next: Finish <span aria-hidden="true">→</span></a>
        )}
      </div>
    </main>
  );
}

function TriageRow({ prRef, f, s }: { prRef: PRRef; f: CIFailure; s: CISession }) {
  const setSession = useSetSession(prRef);
  const recat = useMutation({ mutationFn: (category: CICategory) => api.ciPatch(prRef, f.id, { category }), onSuccess: setSession });
  return (
    <div className="list-row-wrap">
      <div className="list-row">
        <span className="mark">{busy(f, s) ? <Spinner label="Agent working" /> : done(f) ? "✓" : ""}</span>
        <span className="grow min0">
          <a href={prHref(prRef, "ci", `fix/${encodeURIComponent(f.id)}`)} className="block truncate">
            {f.title || f.name}
          </a>
          <span className="muted tiny">{f.name}{f.workflow && f.workflow !== f.name ? ` · ${f.workflow}` : ""}{done(f) ? ` · ${STATUS_LABEL[f.status]}` : ""}</span>
          {f.reason && <span className="muted tiny block"><Inline text={f.reason} /></span>}
        </span>
        <div className="dec" role="group" aria-label="Category">
          {CATEGORIES.map((k) => (
            <button key={k} className={f.category === k ? "on" : ""} onClick={() => recat.mutate(k)} disabled={recat.isPending}>{CATEGORY_LABEL[k]}</button>
          ))}
        </div>
      </div>
    </div>
  );
}

function DiscardCI({ prRef }: { prRef: PRRef }) {
  const setSession = useSetSession(prRef);
  const discard = useMutation({ mutationFn: () => api.ciDiscard(prRef), onSuccess: setSession });
  return (
    <button className="link small" disabled={discard.isPending} onClick={() => {
      if (confirm("Start over? The triage and any proposed fixes are thrown away. Commits you applied stay on your branch.")) discard.mutate();
    }}>Start over</button>
  );
}

// Fix

function CIFix({ prRef, s, id }: { prRef: PRRef; s: CISession; id?: string }) {
  const list = ordered(s);
  const selected = list.find((f) => f.id === id) ?? list.find((f) => !done(f)) ?? list[0];
  const go = (f?: CIFailure) => { if (f) location.hash = prHref(prRef, "ci", `fix/${encodeURIComponent(f.id)}`); };
  if (!selected) return <main className="page"><p className="muted">No failures.</p></main>;
  return (
    <div className="triage">
      <aside className="queue" aria-label="Failures">
        {[...CATEGORIES, null].map((k) => {
          const rows = list.filter((f) => f.category === k);
          if (!rows.length) return null;
          return (
            <div key={k ?? "none"}>
              <div className="queue-group"><span className={`cat-dot ci-${k ?? "none"}`} />{k ? CATEGORY_LABEL[k] : "Not triaged yet"} · {rows.length}</div>
              {rows.map((f) => (
                <button key={f.id} className={`qrow ${f.id === selected.id ? "sel" : ""}`} onClick={() => go(f)}>
                  <span className="mark">{busy(f, s) ? <Spinner label="Agent working" /> : done(f) ? "✓" : ""}</span>
                  <span className="grow min0">
                    <span className="truncate block">{f.title || f.name}</span>
                    <span className="muted tiny">{f.name}</span>
                  </span>
                </button>
              ))}
            </div>
          );
        })}
      </aside>
      <main className="triage-main">
        <FailureCard key={selected.id} prRef={prRef} f={selected}
          onNav={(dir) => go(list[list.findIndex((f) => f.id === selected.id) + dir])} />
      </main>
    </div>
  );
}

function FailureCard({ prRef, f, onNav }: { prRef: PRRef; f: CIFailure; onNav: (dir: 1 | -1) => void }) {
  const current = useAIName();
  const setSession = useSetSession(prRef);
  const [confirmRerun, setConfirmRerun] = useState(false);
  const askFix = useMutation({ mutationFn: () => api.ciFix(prRef, f.id), onSuccess: setSession });
  const apply = useMutation({ mutationFn: (allowConflicts: boolean) => api.ciFixApply(prRef, f.id, allowConflicts), onSuccess: setSession });
  const applyConflict = editsConflict(apply.error);
  const discard = useMutation({ mutationFn: () => api.ciFixDiscard(prRef, f.id), onSuccess: setSession });
  const retry = useMutation({ mutationFn: () => api.ciFixDiscard(prRef, f.id).then(() => api.ciFix(prRef, f.id)), onSuccess: setSession });
  const rerun = useMutation({ mutationFn: () => api.ciRerun(prRef, f.id), onSuccess: (v) => { setSession(v); setConfirmRerun(false); } });
  const mark = useMutation({ mutationFn: (status: "open" | "fixed" | "dismissed") => api.ciPatch(prRef, f.id, { status }), onSuccess: setSession });
  useEffect(() => { window.scrollTo(0, 0); }, []);
  useEffect(() => {
    const on = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "j") onNav(1);
      else if (e.key === "k") onNav(-1);
    };
    window.addEventListener("keydown", on);
    return () => window.removeEventListener("keydown", on);
  });
  const pending = f.fix && (f.fix.status === "running" || f.fix.status === "ready");
  const err = (askFix.error || rerun.error || mark.error) as Error | null;
  const primary = (c: CICategory) => (f.category === c ? "btn primary" : "btn");

  return (
    <article className="card stack-gap">
      <div className="card-top">
        <div className="row-wrap grow small">
          <strong>{f.name}</strong>
          {f.workflow && f.workflow !== f.name && <span className="muted">{f.workflow}</span>}
          <span className="pill warn">{f.conclusion.toLowerCase()}</span>
          {f.required && <span className="pill">Required</span>}
          {f.category && <span className={`pill cat-pill ci-${f.category}`}>{CATEGORY_LABEL[f.category]}</span>}
          {done(f) && <span className="pill ok">{STATUS_LABEL[f.status]}</span>}
          {f.url && <GitHubLink href={f.url}>Logs on GitHub</GitHubLink>}
        </div>
        <button className="btn sm" onClick={() => onNav(-1)} title="Previous (k)">←</button>
        <button className="btn sm" onClick={() => onNav(1)} title="Next (j)">→</button>
        <KeyHelp><div><span className="kbd">j</span> / <span className="kbd">k</span> next / previous failure</div></KeyHelp>
      </div>
      <h1 className="h-thread">{f.title || f.name}</h1>

      {f.reason && (
        <div className="suggest">
          <strong className="small">{f.ai_name ?? current} suggests: {f.category ? CATEGORY_LABEL[f.category] : "—"}</strong>
          <p className="small"><Inline text={f.reason} /></p>
          {f.files.length > 0 && <p className="small">Likely files: {f.files.map((p) => <span key={p} className="mono">{p} </span>)}</p>}
        </div>
      )}

      {f.annotations.length > 0 && (
        <div>
          <h2 className="h3 h3-icon"><BrandIcon name="github" size={16} />GitHub's annotations</h2>
          <ul className="annotations">
            {f.annotations.map((a, i) => (
              <li key={i} className={a.level === "FAILURE" ? "error-text" : "muted"}>
                {a.path && <span className="mono tiny">{a.path}{a.line ? `:${a.line}` : ""} </span>}
                <span className="small pre-wrap">{a.message}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <h2 className="h3">The step that failed</h2>
        {f.excerpt ? <pre className="code log"><div className="meta pre-wrap">{f.excerpt}</div></pre> : <p className="muted small">No log yet.</p>}
      </div>

      {f.fix && f.status === "open" && (
        <ProposedFix
          fix={f.fix} where="the code" onWhat="on your branch" applyLabel="Apply and commit"
          applyNote="Applying commits them to your checkout of the branch (only these files). Uncommitted edits of yours in them are put back on top; if they'd conflict, you're asked first. Push from Finish."
          onApply={() => apply.mutate(false)} onDiscard={() => discard.mutate()} onRetry={() => retry.mutate()}
          busy={apply.isPending || discard.isPending || retry.isPending || !!applyConflict}
          error={((applyConflict ? null : apply.error) || discard.error || retry.error) as Error | null}
          target={<CheckoutPath prRef={prRef} label="Commits to your checkout" />}
        />
      )}
      {f.fix && f.status === "open" && applyConflict && (
        <EditsConflictPrompt conflict={applyConflict} busy={apply.isPending}
          onProceed={() => apply.mutate(true)} onCancel={() => apply.reset()} />
      )}

      {f.status === "fixed" && (
        <p className="small">
          Fixed{f.commit_sha ? <> in commit <span className="mono">{f.commit_sha.slice(0, 10)}</span></> : " by you"}. Push it from Finish.{" "}
          <button className="link small" onClick={() => mark.mutate("open")}>Reopen</button>
        </p>
      )}
      {f.status === "fixed" && <EditConflicts files={f.edit_conflicts} />}
      {f.status === "rerun" && <p className="small">Re-run requested on GitHub. <button className="link small" onClick={() => mark.mutate("open")}>Reopen</button></p>}
      {f.status === "dismissed" && <p className="small">Dismissed. <button className="link small" onClick={() => mark.mutate("open")}>Reopen</button></p>}

      {f.status === "open" && !pending && (
        <div className="row-wrap">
          <button className={primary("fix")} onClick={() => askFix.mutate()} disabled={askFix.isPending}>Ask {current} to fix it</button>
          {f.job_id !== null && (!confirmRerun ? (
            <button className={primary("rerun")} onClick={() => setConfirmRerun(true)}>Re-run job…</button>
          ) : (
            <span className="info-bar">
              <span className="small">Re-run <strong>{f.name}</strong> on GitHub?</span>
              <button className="btn sm primary" onClick={() => rerun.mutate()} disabled={rerun.isPending}>{rerun.isPending ? "Re-running…" : "Yes, re-run"}</button>
              <button className="btn sm" onClick={() => setConfirmRerun(false)}>Cancel</button>
            </span>
          ))}
          <button className="btn" onClick={() => mark.mutate("fixed")}>I fixed it myself</button>
          <button className={primary("unrelated")} onClick={() => mark.mutate("dismissed")}>Dismiss</button>
        </div>
      )}
      {err && <p className="error small">{err.message}</p>}
    </article>
  );
}

// Finish

function CIFinish({ prRef, s, branch }: { prRef: PRRef; s: CISession; branch: string }) {
  const setSession = useSetSession(prRef);
  const qc = useQueryClient();
  const all = ordered(s);
  const open = all.filter((f) => !done(f));
  const commits = all.filter((f) => f.commit_sha);
  const [confirming, setConfirming] = useState(false);
  const checkout = useCheckoutDetails(prRef);
  const remote = useQuery({ queryKey: ["mergeRemote", prRef.number], queryFn: () => api.mergeRemote(prRef) });
  const behindGitHub = (remote.data?.behind ?? 0) > 0;
  const push = useMutation({
    mutationFn: () => api.ciPush(prRef),
    onSuccess: (v) => { setSession(v); setConfirming(false); qc.invalidateQueries({ queryKey: ["work", prRef.number] }); qc.removeQueries({ queryKey: ciStatusKey(prRef) }); },
  });
  return (
    <main className="page narrow">
      {s.pushed && (
        <ResultCard
          prRef={prRef}
          title="Fixes pushed"
          status="Pushed"
          summary={<>{plural(commits.length, "fix commit")} {commits.length === 1 ? "is" : "are"} on GitHub, and CI runs again there.</>}
          details={[...checkout, ...commits.map((f) => ({ label: `Commit (${f.name})`, value: f.commit_sha }))]}
        />
      )}
      <section className="card stack-gap">
        <div className="row-between"><h1>Finish</h1><DiscardCI prRef={prRef} /></div>
        {open.length > 0 && (
          <div className="banner warn row-wrap">
            <span className="grow">{plural(open.length, "failure")} not dealt with yet.</span>
            <a className="btn sm" href={prHref(prRef, "ci", `fix/${encodeURIComponent(open[0].id)}`)}>Go to them</a>
          </div>
        )}
        <ul className="ci-summary">
          {all.map((f) => (
            <li key={f.id} className="small">
              <span className={`pill ${f.status === "open" ? "warn" : "ok"}`}>{f.status === "open" ? "Open" : STATUS_LABEL[f.status]}</span>{" "}
              <strong>{f.title || f.name}</strong> <span className="muted">· {f.name}</span>
              {f.commit_sha && <span className="mono muted"> · {f.commit_sha.slice(0, 10)}</span>}
            </li>
          ))}
        </ul>
      </section>

      <section className="card stack-gap">
        <h2>Push the fixes</h2>
        {s.pushed ? (
          <p className="small">Pushed. See the confirmation above.</p>
        ) : commits.length === 0 ? (
          <p className="muted small">
            No fixes were committed here, so there's nothing to push.
            {all.some((f) => f.status === "rerun") ? " The re-runs you asked for are already running on GitHub." : ""}
          </p>
        ) : (
          <>
            <p className="small">{plural(commits.length, "fix commit")} on your checkout of <span className="mono">{branch}</span>. Pushing starts CI again on GitHub.</p>
            <CheckoutPath prRef={prRef} />
            {behindGitHub && remote.data ? (
              <GitHubCommits prRef={prRef} branch={branch} r={remote.data} action="Bring them in, then push" onSynced={() => push.mutate()} />
            ) : !confirming ? (
              <div className="row-wrap"><button className="btn primary" onClick={() => setConfirming(true)}>Push to GitHub…</button></div>
            ) : (
              <div className="info-bar">
                <span className="grow small">Push {branch} to origin? This only succeeds if nobody else pushed to the branch meanwhile.</span>
                <button className="btn primary sm" onClick={() => push.mutate()} disabled={push.isPending}>{push.isPending ? "Pushing…" : "Yes, push"}</button>
                <button className="btn sm" onClick={() => setConfirming(false)}>Cancel</button>
              </div>
            )}
            {push.error && <pre className="code error-box"><div>{(push.error as Error).message}</div></pre>}
          </>
        )}
      </section>
    </main>
  );
}
