import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { PRRef } from "../api";
import { api } from "../api";
import { storageGet } from "../lib";

/** Where the PR branch is checked out: the one place your edits, reviewr's commits and
 *  the final push all happen. */
export function WorkPanel({ prRef, bare = false }: { prRef: PRRef; bare?: boolean }) {
  const editor = storageGet("reviewr:editor") || "vscode";
  const [opened, setOpened] = useState<string>();
  const work = useQuery({ queryKey: ["work", prRef.number], queryFn: () => api.work(prRef), refetchInterval: 5000 });
  const w = work.data;
  if (!w) return null;

  const open = async () => {
    try {
      const res = await api.open(prRef, { editor });
      setOpened(`Opened: ${res.argv.join(" ")}`);
      work.refetch();
    } catch (e) {
      setOpened((e as Error).message);
    }
  };

  return (
    <section className={bare ? "env-panel bare" : "card env-panel"}>
      <div className="row-wrap">
        <strong className="small">Working in</strong>
        {w.path ? (
          <>
            <span className="mono small">{w.path}</span>
            <span className="pill">{w.branch ?? "detached at the PR head"}</span>
            {!w.branch && <span className="muted tiny">Commits made here are pushed to {w.expected_branch}.</span>}
            {w.created_by_reviewr && <span className="muted tiny">(created by reviewr; you had no checkout of this branch)</span>}
          </>
        ) : (
          <span className="muted small">No checkout of <span className="mono">{w.branch}</span> yet. Opening it creates one.</span>
        )}
        <span className="grow" />
        <button className="btn sm" onClick={open}>Open in {editor}</button>
      </div>
      {w.path && (
        <div className="muted tiny">
          {w.uncommitted?.length ? `${w.uncommitted.length} file${w.uncommitted.length === 1 ? "" : "s"} with uncommitted changes` : "No uncommitted changes"}
          {" · "}{w.ahead ? `${w.ahead} commit${w.ahead === 1 ? "" : "s"} not on GitHub yet` : "nothing unpushed"}
          {w.behind ? <span className="error"> · {w.behind} commit{w.behind === 1 ? "" : "s"} on GitHub you don't have; pull before submitting</span> : null}
          {w.branch && w.expected_branch && w.branch !== w.expected_branch && (
            <span className="error"> · expected {w.expected_branch}</span>
          )}
        </div>
      )}
      {!bare && (
      <p className="muted tiny">
          Your edits here are what reviewr checks and commits, and Respond pushes this branch. reviewr never stages your
          other changes: agent patches commit only their own files.
        </p>
      )}
      {opened && <p className="muted tiny">{opened}</p>}
    </section>
  );
}
