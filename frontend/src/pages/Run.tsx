import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import type { PRContext } from "../App";
import {
  api, setupNeedsConfirm, type PRRef, type RunConfigData, type RunService, type RunSession, type RunTarget, type RunView,
  type SetupNeedsConfirm,
} from "../api";
import { CopyField } from "../components/CopyField";
import { Spinner } from "../components/Spinner";

// Run the PR's app (backend/reviewr/run.py): its services at the PR branch, started,
// watched and stopped from here.

export const runKey = (ref: PRRef) => ["run", ref.owner, ref.repo, ref.number];
const busy = (s?: RunSession | null) => !!s && (s.status === "starting" || s.status === "stopping");
export const isRunning = (s?: RunSession | null) => !!s && (s.status === "starting" || s.status === "running");

/** The PR's run, polled quickly while it starts or stops and slowly while it runs. */
export function useRun(ref: PRRef) {
  return useQuery({
    queryKey: runKey(ref), queryFn: () => api.run(ref), retry: false,
    refetchInterval: (q) => (busy(q.state.data?.session) ? 1000 : isRunning(q.state.data?.session) ? 5000 : false),
  });
}

const ICON: Record<string, string> = { pending: "○", running: "◐", done: "✓", skipped: "–", warning: "!", failed: "✕" };
const STATUS_TEXT: Record<RunSession["status"], string> = {
  starting: "Starting", running: "Running", stopping: "Stopping", stopped: "Stopped", failed: "Failed",
};

export function Run({ ctx }: { ctx: PRContext }) {
  const { pr } = ctx.data!;
  const qc = useQueryClient();
  const run = useRun(pr.ref);
  const set = (v: RunView) => qc.setQueryData(runKey(pr.ref), v);
  const v = run.data;
  const s = v?.session;

  return (
    <main className="page narrow">
      <div className="page-head">
        <div className="grow">
          <h1>Run the app</h1>
          <p className="muted">Start {pr.ref.repo} at <span className="mono">{pr.head_ref}</span>, try it in your browser, then stop it.</p>
        </div>
      </div>
      {run.error && <div className="banner error">{(run.error as Error).message}</div>}
      {v && !v.config && v.suggestion && <FirstRun prRef={pr.ref} suggestion={v.suggestion} onSaved={set} />}
      {v?.config && (
        <>
          {s && s.status !== "stopped" ? (
            <SessionCard prRef={pr.ref} view={v} session={s} onChange={set} />
          ) : (
            <StartCard ctx={ctx} view={v} onChange={set} />
          )}
          <ConfigCard prRef={pr.ref} config={v.config} onSaved={set} />
        </>
      )}
    </main>
  );
}

/** No saved config yet: show what was detected, editable, to save before anything runs. */
function FirstRun({ prRef, suggestion, onSaved }: {
  prRef: PRRef; suggestion: NonNullable<RunView["suggestion"]>; onSaved: (v: RunView) => void;
}) {
  return (
    <section className="card stack-gap">
      <h2>How to run it</h2>
      <p className="small">
        reviewr read the files on the PR branch and proposes the commands below. Nothing runs until you save them; they're kept for
        every PR of this repository and you can change them later here or in Settings.
      </p>
      {suggestion.notes.length > 0 && (
        <ul className="small note-list">{suggestion.notes.map((n) => <li key={n}>{n}</li>)}</ul>
      )}
      <ConfigEditor prRef={prRef} initial={suggestion.config} action="Save" onSaved={onSaved} />
    </section>
  );
}

function ConfigEditor({ prRef, initial, action, onSaved, onCancel }: {
  prRef: PRRef; initial: RunConfigData; action: string; onSaved: (v: RunView) => void; onCancel?: () => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(initial, null, 2));
  const [parseError, setParseError] = useState<string>();
  const save = useMutation({ mutationFn: (c: RunConfigData) => api.runSaveConfig(prRef, c), onSuccess: onSaved });
  const submit = () => {
    try { save.mutate(JSON.parse(text)); setParseError(undefined); } catch (e) { setParseError((e as Error).message); }
  };
  return (
    <div className="stack-gap">
      <p className="muted small">
        <span className="mono">services</span> run until you stop them (argv lists, not shell strings; <span className="mono">port</span> is how
        reviewr knows one is ready). <span className="mono">setup</span> commands run first, in order. <span className="mono">env</span> is set
        for all of them, over your env files: use it to point the app at its own database so the PR's migrations don't change one you share.
      </p>
      <textarea className="input mono" rows={18} value={text} spellCheck={false} onChange={(e) => setText(e.target.value)} />
      {(parseError || save.error) && <p className="error small">{parseError ?? (save.error as Error).message}</p>}
      <div className="row-wrap">
        <button className="btn primary" onClick={submit} disabled={save.isPending}>{action}</button>
        {onCancel && <button className="btn" onClick={onCancel}>Cancel</button>}
      </div>
    </div>
  );
}

function ConfigCard({ prRef, config, onSaved }: { prRef: PRRef; config: RunConfigData; onSaved: (v: RunView) => void }) {
  const [editing, setEditing] = useState(false);
  const detect = useQuery({ queryKey: ["runDetect", ...runKey(prRef)], queryFn: () => api.run(prRef, true), enabled: false });
  const [base, setBase] = useState<RunConfigData>(config);
  const [version, setVersion] = useState(0);  // a new editor when a detection replaces its text
  if (editing) {
    return (
      <section className="card stack-gap">
        <div className="row-between">
          <h2>How to run it</h2>
          <button className="link small" disabled={detect.isFetching}
            onClick={() => detect.refetch().then((r) => { if (r.data?.suggestion) { setBase(r.data.suggestion.config); setVersion((n) => n + 1); } })}>
            {detect.isFetching ? "Detecting…" : "Detect again from this branch"}
          </button>
        </div>
        {detect.data?.suggestion && (
          <ul className="small note-list">{detect.data.suggestion.notes.map((n) => <li key={n}>{n}</li>)}</ul>
        )}
        <ConfigEditor key={version} prRef={prRef} initial={base} action="Save" onCancel={() => setEditing(false)}
          onSaved={(v) => { onSaved(v); setEditing(false); }} />
      </section>
    );
  }
  return (
    <section className="card stack-gap">
      <div className="row-between">
        <h2>How to run it</h2>
        <button className="link small" onClick={() => { setBase(config); setEditing(true); }}>Edit</button>
      </div>
      <dl className="details small">
        {config.services.map((svc) => (
          <Row key={svc.name} label={svc.name}>
            <span className="mono">{svc.cwd !== "." ? `(${svc.cwd}) ` : ""}{svc.run.join(" ")}</span>
            {svc.port ? <span className="muted"> · port {svc.port}</span> : <span className="muted"> · no port</span>}
          </Row>
        ))}
        {config.compose && <Row label="Docker Compose"><span className="mono">{config.compose.file}</span>{config.compose.services.length ? ` (${config.compose.services.join(", ")})` : ""}</Row>}
        {config.setup.map((c) => <Row key={c.name} label={`Setup: ${c.name}`}><span className="mono">{c.run.join(" ")}</span></Row>)}
        {config.env_files.length > 0 && <Row label="Env files"><span className="mono">{config.env_files.join(", ")}</span> <span className="muted">(copied from your clone when missing)</span></Row>}
        {Object.keys(config.env).length > 0 && <Row label="Env overrides"><span className="mono">{Object.keys(config.env).join(", ")}</span></Row>}
      </dl>
    </section>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return <><dt className="muted">{label}</dt><dd className="min0">{children}</dd></>;
}

function StartCard({ ctx, view, onChange }: { ctx: PRContext; view: RunView; onChange: (v: RunView) => void }) {
  const { pr } = ctx.data!;
  const [target, setTarget] = useState<RunTarget>("github");
  const work = useQuery({ queryKey: ["work", pr.ref.number], queryFn: () => api.work(pr.ref) });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const login = settings.data?.github_login;
  const theirs = !!login && pr.author.toLowerCase() !== login.toLowerCase();
  const [acknowledged, setAcknowledged] = useState(false);
  const start = useMutation({
    mutationFn: (setup: "ask" | "run" | "skip") => api.runStart(pr.ref, target, setup),
    onSuccess: onChange,
  });
  const confirm = setupNeedsConfirm(start.error);
  const w = work.data;
  const local = [
    w?.ahead ? `${w.ahead} unpushed commit${w.ahead === 1 ? "" : "s"}` : null,
    w?.uncommitted?.length ? `${w.uncommitted.length} edited file${w.uncommitted.length === 1 ? "" : "s"}` : null,
  ].filter(Boolean);
  const s = view.session;

  return (
    <section className="card stack-gap">
      <h2>Start</h2>
      {s?.status === "stopped" && s.error && <p className="muted small">Last run: {s.error}</p>}
      <div className="radio-list" role="radiogroup" aria-label="Code to run">
        <label className="radio-row">
          <input type="radio" checked={target === "github"} onChange={() => setTarget("github")} />
          <span><strong>The PR on GitHub</strong> <span className="mono muted">{pr.head_oid.slice(0, 7)}</span>
            <span className="muted small block">What reviewers see, in a separate copy. Your checkout isn't touched.</span></span>
        </label>
        <label className="radio-row">
          <input type="radio" checked={target === "checkout"} onChange={() => setTarget("checkout")} disabled={!w?.path} />
          <span><strong>Your checkout</strong>{local.length ? <span className="muted"> · {local.join(", ")}</span> : null}
            <span className="muted small block">
              {w?.path ? <>Includes your unpushed commits and edits, in <span className="mono">{w.path}</span>.</> : "You don't have a checkout of this branch yet."}
            </span></span>
        </label>
      </div>
      {theirs && !acknowledged ? (
        <div className="warn-box small stack-gap">
          <p>
            This runs code from <strong>{pr.author}</strong>'s branch on your machine, with the env files copied from your clone. Read the
            diff first if you don't know what it does.
          </p>
          <div className="row-wrap"><button className="btn sm" onClick={() => setAcknowledged(true)}>I understand</button></div>
        </div>
      ) : (
        <div className="row-wrap">
          <button className="btn primary" onClick={() => start.mutate("ask")} disabled={start.isPending || !!confirm}>
            {start.isPending ? "Starting…" : "Start"}
          </button>
        </div>
      )}
      {confirm ? (
        <SetupConfirm confirm={confirm} busy={start.isPending}
          onRun={() => start.mutate("run")} onSkip={() => start.mutate("skip")} onCancel={() => start.reset()} />
      ) : start.error && <p className="error small pre-wrap">{(start.error as Error).message}</p>}
    </section>
  );
}

/** The database guard: setup commands would run against a database that isn't local. */
function SetupConfirm({ confirm, busy, onRun, onSkip, onCancel }: {
  confirm: SetupNeedsConfirm; busy: boolean; onRun: () => void; onSkip: () => void; onCancel: () => void;
}) {
  const remote = confirm.databases.filter((d) => !d.local);
  return (
    <div className="warn-box small stack-gap">
      <p><strong>The setup commands might change a database other people use.</strong> Nothing has started yet.</p>
      <p>Commands: {confirm.commands.map((c, i) => <span key={c}>{i > 0 && ", "}<span className="mono">{c}</span></span>)}.</p>
      {remote.length > 0 ? (
        <ul className="note-list">
          {remote.map((d) => <li key={d.var}><span className="mono">{d.var}</span> points at <span className="mono">{d.host}</span></li>)}
        </ul>
      ) : (
        <p>reviewr couldn't find which database they use (no DATABASE_URL or DB host variable in your env files or overrides).</p>
      )}
      <p>
        To run them safely every time, add env overrides under "How to run it" that point the app at its own database, for example the
        one Docker Compose starts.
      </p>
      <div className="row-wrap">
        <button className="btn sm" onClick={onSkip} disabled={busy}>Start without setup</button>
        <button className="btn sm" onClick={onRun} disabled={busy}>Run setup anyway</button>
        <button className="link small" onClick={onCancel} disabled={busy}>Cancel</button>
      </div>
    </div>
  );
}

function SessionCard({ prRef, view, session: s, onChange }: {
  prRef: PRRef; view: RunView; session: RunSession; onChange: (v: RunView) => void;
}) {
  const stop = useMutation({ mutationFn: (removeData: boolean) => api.runStop(prRef, removeData), onSuccess: onChange });
  const restart = useMutation({
    mutationFn: () => api.runStop(prRef).then(() => api.runStart(prRef, s.target, s.setup_skipped ? "skip" : "ask")),
    onSuccess: onChange,
  });
  const app = s.services.find((x) => x.open && x.url) ?? s.services.find((x) => x.url);
  const tone = s.status === "running" ? "ok" : s.status === "failed" ? "warn" : "";
  return (
    <section className="card stack-gap">
      <div className="row-between">
        <h2 className="row-wrap">
          {busy(s) && <Spinner />}
          {s.target === "github" ? "The PR on GitHub" : "Your checkout"}
          <span className={`pill ${tone}`}>{STATUS_TEXT[s.status]}</span>
        </h2>
        <div className="row-wrap">
          {app && s.status === "running" && <a className="btn primary sm" href={app.url!} target="_blank" rel="noreferrer">Open {app.url}</a>}
          <button className="btn sm" onClick={() => restart.mutate()} disabled={restart.isPending || stop.isPending || busy(s)}>Restart</button>
          <button className="btn sm" onClick={() => stop.mutate(false)} disabled={stop.isPending || s.status === "stopping"}>
            {stop.isPending ? "Stopping…" : "Stop"}
          </button>
        </div>
      </div>
      {view.head_moved && (
        <div className="info-bar small">
          <span className="grow">The PR has new commits since this started at <span className="mono">{s.head_sha.slice(0, 7)}</span>.</span>
          <button className="btn sm" onClick={() => restart.mutate()} disabled={restart.isPending}>Restart on the new head</button>
        </div>
      )}
      {s.error && <p className="error small pre-wrap">{s.error}</p>}
      {(stop.error || restart.error) && <p className="error small pre-wrap">{((stop.error || restart.error) as Error).message}</p>}
      <div className="row-wrap small"><span className="muted">Running in</span><CopyField value={s.worktree} label="Folder" /></div>

      <div className="stack-gap">
        {s.services.map((svc) => <ServiceRow key={svc.name} prRef={prRef} svc={svc} />)}
      </div>

      <details open={s.status === "starting" || s.status === "failed"}>
        <summary className="small">Steps</summary>
        <ol className="setup-steps details-body">
          {s.steps.map((st) => (
            <li key={st.id} className={`setup-step ${st.status}`}>
              <span className="setup-icon" aria-label={st.status}>{ICON[st.status]}</span>
              <div className="grow min0">
                <div className="strong small">{st.title}</div>
                {st.detail && <div className="muted small pre-wrap">{st.detail}</div>}
                {st.log && <details><summary className="small">Output</summary><pre className="cmd">{st.log}</pre></details>}
              </div>
            </li>
          ))}
        </ol>
      </details>
      {s.compose_project && (
        <p className="muted small">
          Docker Compose project <span className="mono">{s.compose_project}</span>. Stop keeps its data;{" "}
          <button className="link small" onClick={() => { if (confirm("Stop and delete the Compose volumes (its databases)?")) stop.mutate(true); }}>
            stop and remove its data
          </button>.
        </p>
      )}
    </section>
  );
}

function ServiceRow({ prRef, svc }: { prRef: PRRef; svc: RunService }) {
  const [showLog, setShowLog] = useState(svc.status === "exited");
  const tone = svc.status === "running" ? "ok" : svc.status === "exited" ? "warn" : "";
  return (
    <div className="stack-gap">
      <div className="row-wrap">
        <strong>{svc.name}</strong>
        <span className={`pill ${tone}`}>
          {svc.status === "starting" && <><Spinner size={10} />&nbsp;</>}
          {svc.status}{svc.status === "exited" && svc.exit_code !== null ? ` (${svc.exit_code})` : ""}
        </span>
        {svc.url && svc.status === "running" && <a href={svc.url} target="_blank" rel="noreferrer" className="small mono">{svc.url}</a>}
        <span className="grow" />
        <button className="link small" onClick={() => setShowLog(!showLog)}>{showLog ? "Hide log" : "Log"}</button>
      </div>
      {svc.note && <p className="muted small">{svc.note}</p>}
      {showLog && <ServiceLog prRef={prRef} name={svc.name} live={svc.status === "starting" || svc.status === "running"} />}
    </div>
  );
}

function ServiceLog({ prRef, name, live }: { prRef: PRRef; name: string; live: boolean }) {
  const log = useQuery({
    queryKey: ["runLog", ...runKey(prRef), name], queryFn: () => api.runLog(prRef, name),
    refetchInterval: live ? 2000 : false,
  });
  const [el, setEl] = useState<HTMLPreElement | null>(null);
  useEffect(() => { if (el) el.scrollTop = el.scrollHeight; }, [el, log.data?.text]);
  return (
    <pre className="code log" ref={setEl}>
      <div className="meta pre-wrap">{log.data?.text || (log.isLoading ? "Loading…" : "(nothing yet)")}</div>
    </pre>
  );
}

/** The header's Run button: shows whether the PR's app is running. */
export function RunButton({ prRef, on }: { prRef: PRRef; on: boolean }) {
  const s = useRun(prRef).data?.session;
  const title = s && isRunning(s) ? `The app is ${STATUS_TEXT[s.status].toLowerCase()}` : "Run the PR's app";
  return (
    <a href={`#/pr/${prRef.owner}/${prRef.repo}/${prRef.number}/run`} className={`btn sm run-btn ${on ? "on" : ""}`} title={title}>
      {busy(s) ? <Spinner size={10} /> : s?.status === "running" ? <span className="run-dot" aria-hidden="true" /> : null}
      Run
    </a>
  );
}
