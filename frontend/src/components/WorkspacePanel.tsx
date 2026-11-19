import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type PRRef } from "../api";
import { EnvPanel } from "./EnvPanel";
import { WorkPanel } from "./WorkPanel";

/** The PR's workspace: the checkout of its branch and the installed dependencies.
 *  Shown in the header on every step as a one-line status that opens the details. */
export function WorkspaceMenu({ prRef, headRef }: { prRef: PRRef; headRef: string }) {
  // Same query keys as WorkPanel and EnvPanel, so these share their requests.
  const work = useQuery({ queryKey: ["work", prRef.number], queryFn: () => api.work(prRef) });
  const envs = useQuery({ queryKey: ["envs", prRef.owner, prRef.repo, prRef.number], queryFn: () => api.envs(prRef) });
  const list = envs.data ?? [];
  const ready = list.filter((e) => e.status === "ready").length;
  const place = work.data?.path ? work.data.path.split("/").slice(-1)[0] : "no checkout yet";
  const deps = list.length ? (ready === list.length ? "dependencies installed" : `${ready} of ${list.length} dependencies installed`) : "";

  // Close when clicking anywhere outside the menu.
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current?.open && !ref.current.contains(e.target as Node)) ref.current.open = false;
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, []);

  return (
    <details className="workspace-menu" ref={ref}>
      <summary title={work.data?.path ?? ""}>
        <span className="strong">Workspace</span>
        <span className="muted"> · {place}{deps && ` · ${deps}`}</span>
        <span aria-hidden="true" className="muted"> ▾</span>
      </summary>
      <div className="workspace-pop stack-gap">
        <p className="muted small">
          Fixes are committed on the PR branch, <span className="mono">{headRef}</span>, in your checkout of it; reviewr creates one if you
          don't have one. Your edits there are what reviewr checks and commits, and Respond pushes the branch with the replies.
        </p>
        <WorkPanel prRef={prRef} bare />
        <EnvPanel prRef={prRef} bare />
      </div>
    </details>
  );
}
