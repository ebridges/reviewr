import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type CleanupItem } from "../api";

const gb = (mb: number) => (mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb} MB`);

/** Dependency installs and worktrees on disk, with a previewed cleanup. */
export function StoragePanel() {
  const [asked, setAsked] = useState(false);
  const preview = useQuery({ queryKey: ["cleanup"], queryFn: api.cleanupPlan, enabled: asked });
  const run = useMutation({ mutationFn: api.cleanup, onSuccess: () => preview.refetch() });
  const d = preview.data;
  const removing = d ? d.plan.envs_remove.length + d.plan.worktrees_remove.length : 0;

  return (
    <section className="card stack-gap">
      <div className="row-between">
        <h3 className="h3">Storage</h3>
        <button className="btn sm" onClick={() => (asked ? preview.refetch() : setAsked(true))} disabled={preview.isFetching}>
          {preview.isFetching ? "Checking…" : d ? "Check again" : "Check what can be removed"}
        </button>
      </div>
      <p className="muted small">
        Dependency installs are shared: every worktree links to one install per set of lockfiles. A cleanup removes installs
        no open PR needs, and reviewr's worktrees for merged or closed PRs when nothing on them is unpushed. Checking asks
        GitHub whether each PR is still open.
      </p>
      {preview.error && <p className="error small">{(preview.error as Error).message}</p>}
      {d && (
        <>
          <p className="small">
            <strong>{gb(d.keep_mb)}</strong> in use by {d.plan.open_prs.length} open PR{d.plan.open_prs.length === 1 ? "" : "s"}.{" "}
            {removing ? <><strong>{gb(d.remove_mb)}</strong> in {removing} item{removing === 1 ? "" : "s"} can be removed.</> : "Nothing to remove."}
          </p>
          <p className="muted tiny">Sizes come from du, which counts copy-on-write clones in full, so the real total is lower.</p>
          <ItemList title="Will be removed" items={[...d.plan.envs_remove, ...d.plan.worktrees_remove]} />
          <ItemList title="Kept" items={[...d.plan.envs_keep, ...d.plan.worktrees_keep]} />
          {d.plan.unknown_prs.length > 0 && (
            <p className="muted small">Couldn't reach GitHub for {d.plan.unknown_prs.join(", ")}; treated as open and kept.</p>
          )}
          {removing > 0 && (
            <button className="btn" disabled={run.isPending}
              onClick={() => { if (confirm(`Delete ${removing} item${removing === 1 ? "" : "s"} (${gb(d.remove_mb)})? Removed installs are reinstalled automatically if needed again.`)) run.mutate(); }}>
              {run.isPending ? "Cleaning up…" : `Clean up ${gb(d.remove_mb)}`}
            </button>
          )}
          {run.data && <ul className="small">{run.data.done.map((x) => <li key={x}>{x}</li>)}</ul>}
          {run.error && <p className="error small">{(run.error as Error).message}</p>}
        </>
      )}
    </section>
  );
}

function ItemList({ title, items }: { title: string; items: CleanupItem[] }) {
  if (!items.length) return null;
  return (
    <details open={title === "Will be removed"}>
      <summary className="small">{title} · {items.length}</summary>
      <ul className="commits">
        {items.map((i) => (
          <li key={i.path}>
            <span className="mono">{i.label ?? i.path.split("/").slice(-2).join("/")}</span>
            {i.size_mb !== undefined && <span className="muted"> · {gb(i.size_mb)}</span>}
            <span className="muted"> · {i.reason}</span>
          </li>
        ))}
      </ul>
    </details>
  );
}
