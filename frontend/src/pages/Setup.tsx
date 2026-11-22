import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { api, type PRRef, type SetupStep, type SetupView } from "../api";

const ICON: Record<SetupStep["status"], string> = {
  pending: "○", running: "◐", done: "✓", skipped: "–", warning: "!", failed: "✕",
};

/** Shown instead of the workflow when the PR's repository isn't set up yet: clone (or
 *  find a clone), check out the branch, find Node and Python, install dependencies. */
export function Setup({ prRef, title, setup }: { prRef: PRRef; title?: string; setup: UseQueryResult<SetupView> }) {
  const qc = useQueryClient();
  const start = useMutation({
    mutationFn: () => api.startSetup(prRef),
    onSuccess: (v) => qc.setQueryData(["setup", prRef.owner, prRef.repo, prRef.number], v),
  });
  const v = setup.data;
  const session = v?.session;
  const running = !!v?.running;
  const finished = session && !running;
  const slug = `${prRef.owner}/${prRef.repo}`;

  const done = () => {
    qc.invalidateQueries();  // everything that needed the clone can load now
  };

  return (
    <main className="page narrow setup">
      <div className="page-head">
        <div className="grow">
          <h1>Set up the workspace</h1>
          <p className="muted">
            {title ? <>#{prRef.number} · {title}. </> : null}
            {v?.needed ?? `Getting ${slug} ready.`}
          </p>
        </div>
      </div>

      {!running && !finished && (
        <section className="card stack-gap">
          <p>
            reviewr looks for a clone of <span className="mono">{slug}</span> in <span className="mono">{v?.workspace_dir}</span>, or clones it to{" "}
            <span className="mono">{v?.workspace_dir}/{slug}</span>. Then it checks out the PR branch, finds the Node.js and Python versions the
            repository asks for (installing them if you have nvm, fnm, mise or uv), and installs its dependencies from the branch's lockfiles.
            Steps that don't depend on each other run at the same time.
          </p>
          <p className="muted small">
            Your own checkout is never switched or changed. If you already have a clone somewhere else, add it under{" "}
            <a href="#/settings">Settings → Workspace</a> instead, or change the workspace folder there.
          </p>
          <div className="row-wrap">
            <button className="btn primary" onClick={() => start.mutate()} disabled={start.isPending}>Set up {slug}</button>
          </div>
        </section>
      )}

      {start.error && <div className="banner error">{(start.error as Error).message}</div>}

      {session && (
        <section className="card stack-gap">
          <ol className="setup-steps">
            {session.steps.map((st) => (
              <li key={st.id} className={`setup-step ${st.status}`}>
                <span className="setup-icon" aria-label={st.status}>{ICON[st.status]}</span>
                <div className="grow min0">
                  <div className="strong">{st.title}</div>
                  {st.detail && <div className="muted small pre-wrap">{st.detail}</div>}
                  {st.log && (st.status === "failed" || st.status === "warning") && (
                    <details><summary className="small">Output</summary><pre className="cmd">{st.log}</pre></details>
                  )}
                </div>
              </li>
            ))}
          </ol>
          {running && <p className="muted small">Setting up… you can leave this page; setup keeps running.</p>}
          {finished && session.status === "done" && (
            <div className="row-wrap">
              <span className="pill ok">Ready</span>
              <button className="btn primary" onClick={done}>Continue to the pull request</button>
            </div>
          )}
          {finished && session.status === "failed" && (
            <div className="stack-gap">
              <p className="error small">{session.error}</p>
              <div className="row-wrap">
                <button className="btn" onClick={() => start.mutate()} disabled={start.isPending}>Run setup again</button>
                {!v?.needed && <button className="btn primary" onClick={done}>Continue anyway</button>}
              </div>
            </div>
          )}
        </section>
      )}
    </main>
  );
}
