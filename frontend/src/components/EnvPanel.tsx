import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { PRRef } from "../api";
import { api } from "../api";

const LABEL = { ready: "Installed", missing: "Not installed", installing: "Installing…", failed: "Install failed" } as const;

/** The branch's dependency environments that drafts and checks use. */
export function EnvPanel({ prRef, bare = false }: { prRef: PRRef; bare?: boolean }) {
  const qc = useQueryClient();
  const key = ["envs", prRef.owner, prRef.repo, prRef.number];
  const envs = useQuery({
    queryKey: key,
    queryFn: () => api.envs(prRef),
    refetchInterval: (q) => (q.state.data?.some((e) => e.status === "installing") ? 3000 : false),
  });
  const install = useMutation({ mutationFn: () => api.installEnvs(prRef), onSuccess: (d) => qc.setQueryData(key, d) });
  const list = envs.data ?? [];
  if (!list.length) return null;
  const needed = list.filter((e) => e.status === "missing" || e.status === "failed");

  return (
    <section className={bare ? "env-panel bare" : "card env-panel"}>
      <div className="row-wrap">
        <strong className="small">Branch dependencies</strong>
        {list.map((e) => (
          <span key={e.name} className={`pill ${e.status === "ready" ? "ok" : e.status === "failed" ? "warn" : ""}`} title={e.path}>
            {e.name}: {LABEL[e.status]}
          </span>
        ))}
        <span className="grow" />
        {needed.length > 0 && (
          <button className="btn sm" onClick={() => install.mutate()} disabled={install.isPending}>
            Install {needed.map((e) => e.name).join(" and ")}
          </button>
        )}
      </div>
      <p className="muted tiny">
        Installed from this PR's lockfiles, not your clone, and reused by any branch with the same lockfiles. Checks install
        them automatically if needed; installing ahead saves the wait.
      </p>
      {list.filter((e) => e.status === "failed" || e.status === "installing").map((e) => e.log_tail && (
        <details key={e.name} open={e.status === "failed"}>
          <summary className="tiny">{e.name} install log</summary>
          <pre className="code"><div className="meta">{e.log_tail}</div></pre>
        </details>
      ))}
      {install.error && <p className="error small">{(install.error as Error).message}</p>}
    </section>
  );
}
