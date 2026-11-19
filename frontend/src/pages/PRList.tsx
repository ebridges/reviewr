import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, parsePrRef, type PRSummary } from "../api";
import { prHref } from "../lib";

const REVIEW_LABEL: Record<string, string> = {
  CHANGES_REQUESTED: "Changes requested",
  APPROVED: "Approved",
  REVIEW_REQUIRED: "Review required",
};

function ago(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

export function PRList() {
  const [which, setWhich] = useState<"mine" | "review">("mine");
  const [url, setUrl] = useState("");
  const prs = useQuery({ queryKey: ["prs", which], queryFn: () => api.prs(which), staleTime: 30_000 });

  const openUrl = () => {
    const ref = parsePrRef(url);
    if (ref) location.hash = prHref(ref);
  };

  return (
    <main className="page">
      <div className="page-head">
        <h1>Pull requests</h1>
        <span className="spacer" />
        <label className="small muted" htmlFor="pr-url">Open by URL</label>
        <input
          id="pr-url"
          className="input url-input"
          value={url}
          placeholder="https://github.com/owner/repo/pull/123"
          onChange={(e) => setUrl(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && openUrl()}
        />
        <button className="btn" onClick={openUrl} disabled={!parsePrRef(url)}>Open</button>
      </div>

      <div role="tablist" className="tabs">
        <button role="tab" aria-selected={which === "mine"} className={which === "mine" ? "on" : ""} onClick={() => setWhich("mine")}>
          My open PRs
        </button>
        <button role="tab" aria-selected={which === "review"} className={which === "review" ? "on" : ""} onClick={() => setWhich("review")}>
          Review requested from me
        </button>
      </div>

      {prs.error && <div className="banner error">{(prs.error as Error).message}</div>}
      {prs.isLoading && <p className="muted">Loading from GitHub…</p>}
      {prs.data && prs.data.length === 0 && <p className="muted">No open pull requests here.</p>}
      {prs.data && prs.data.length > 0 && (
        <div className="card list">
          {prs.data.map((p) => (
            <div key={`${p.ref.owner}/${p.ref.repo}#${p.ref.number}`} className="pr-row-wrap">
            <a className="pr-row" href={prHref(p.ref)}>
              <div className="grow">
                <div className="strong">{p.title}</div>
                <div className="muted small">
                  {p.ref.owner}/{p.ref.repo} #{p.ref.number} · <span className="mono">{p.head_ref}</span> ·{" "}
                  {p.changed_files} files · updated {ago(p.updated_at)}
                </div>
              </div>
              <span className={`pill ${p.is_draft ? "" : p.review_decision === "CHANGES_REQUESTED" ? "warn" : p.review_decision === "APPROVED" ? "ok" : ""}`}>
                {p.is_draft ? "Draft" : REVIEW_LABEL[p.review_decision ?? ""] ?? "No reviews"}
              </span>
              <span className="w-threads"><strong>{p.open_threads}</strong> <span className="muted small">open threads</span></span>
              <span className={`small w-ci ${p.ci === "SUCCESS" ? "ok-text" : p.ci === "FAILURE" || p.ci === "ERROR" ? "error" : "muted"}`}>
                {p.ci === "SUCCESS" ? "CI passing" : p.ci === "FAILURE" || p.ci === "ERROR" ? "CI failing" : p.ci ? "CI pending" : ""}
              </span>
            </a>
            {which === "mine" && <StatusMenu pr={p} />}
            </div>
          ))}
        </div>
      )}
    </main>
  );
}


type Status = "draft" | "ready" | "closed";

const CONFIRM: Record<Status, (n: number) => string> = {
  ready: (n) => `Mark #${n} ready for review on GitHub? Its requested reviewers are notified.`,
  draft: (n) => `Convert #${n} to a draft on GitHub? Reviewers stay assigned but aren't asked to review until it's ready again.`,
  closed: (n) => `Close #${n} on GitHub without merging? It leaves this list. You can reopen it on GitHub.`,
};

/** A PR's status on GitHub: draft, ready for review, or closed. Every change asks first. */
function StatusMenu({ pr }: { pr: PRSummary }) {
  const qc = useQueryClient();
  const current: Status = pr.is_draft ? "draft" : "ready";
  const change = useMutation({
    mutationFn: (status: Status) => api.setStatus(pr.ref, pr.node_id, status),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["prs"] }),
  });
  return (
    <div className="draft-switch">
      <select
        className="input"
        aria-label={`Status of #${pr.ref.number}`}
        value={change.isPending ? "" : current}
        disabled={change.isPending || !pr.node_id}
        onChange={(e) => {
          const next = e.target.value as Status;
          if (next !== current && confirm(CONFIRM[next](pr.ref.number))) change.mutate(next);
        }}
      >
        {change.isPending && <option value="">Updating…</option>}
        <option value="draft">Draft</option>
        <option value="ready">Ready for review</option>
        <option value="closed">Closed</option>
      </select>
      {change.error && <span className="error tiny">{(change.error as Error).message}</span>}
    </div>
  );
}
