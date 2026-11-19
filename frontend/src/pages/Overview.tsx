import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import type { PRContext } from "../App";
import { api, type Decision, type Thread } from "../api";
import { GROUP_LABEL, decisionLabel, excerpt, prHref, threadTitle, useAIName, where } from "../lib";
import { BranchStatusCard } from "./Merge";

const GROUP_DETAIL: Record<Decision, string> = {
  fix: "Clear requests for a code change. These become work items in Plan.",
  reply: "Answer in a reply; no code change expected.",
  push_back: "The request may conflict with another comment or with the PR's goal.",
  already_done: "{ai} thinks these are already handled. Confirm each one.",
  defer: "Reasonable, but larger than this PR. Candidates for a follow-up ticket.",
  no_action: "Nothing to change or answer, such as your own notes for reviewers. Confirm in Triage and nothing is posted.",
};

export function Overview({ ctx }: { ctx: PRContext }) {
  const { pr, state } = ctx.data!;
  const aiName = useAIName();
  const open = pr.threads.filter((t) => !t.is_resolved);
  const stale = open.filter((t) => t.is_outdated);
  const hasSuggestions = Object.keys(state.suggestions).length > 0;

  const [result, setResult] = useState<string>();
  const suggest = useMutation({
    mutationFn: () => api.suggest(pr.ref, hasSuggestions),
    onSuccess: (st) => {
      ctx.setState(st);
      // Say what came back, so a re-run that changes nothing still visibly finished.
      const counts = new Map<Decision, number>();
      for (const t of open) {
        const d = st.suggestions[t.id]?.decision;
        if (d) counts.set(d, (counts.get(d) ?? 0) + 1);
      }
      const parts = [...counts.entries()].map(([d, n]) => `${n} ${decisionLabel(d)}`);
      const total = [...counts.values()].reduce((a, b) => a + b, 0);
      setResult(`${aiName} suggested a decision for ${total} thread${total === 1 ? "" : "s"}: ${parts.join(", ")}. ${new Date().toLocaleTimeString()}`);
    },
  });

  // Every open thread lands in exactly one group: stale, Claude's suggested decision,
  // or "not yet suggested" (before asking Claude, or threads added since). Empty groups
  // are hidden.
  const allGroups: { key: string; label: string; detail: string; threads: Thread[] }[] = [
    {
      key: "stale",
      label: "Stale",
      detail: "The code under these comments changed after they were written. That doesn't mean they were addressed; each still needs a decision.",
      threads: stale,
    },
    ...(["fix", "reply", "push_back", "defer", "already_done", "no_action"] as Decision[]).map((d) => ({
      key: d, label: GROUP_LABEL[d], detail: GROUP_DETAIL[d].replace("{ai}", aiName),
      threads: open.filter((t) => !t.is_outdated && state.suggestions[t.id]?.decision === d),
    })),
    {
      key: "none",
      label: "Not yet suggested",
      detail: hasSuggestions
        ? `Added since ${aiName} last looked. Ask again, or decide them yourself in Triage.`
        : `Ask ${aiName} above to suggest a decision for each, or decide them yourself in Triage.`,
      threads: open.filter((t) => !t.is_outdated && !state.suggestions[t.id]),
    },
  ];
  const groups = allGroups.filter((g) => g.threads.length > 0);

  const byReviewer = new Map<string, number>();
  for (const t of open) byReviewer.set(t.comments[0]?.author ?? "ghost", (byReviewer.get(t.comments[0]?.author ?? "ghost") ?? 0) + 1);
  const reviewerState = new Map(pr.reviewers.map((r) => [r.login, r.state]));

  const byFile = new Map<string, number>();
  for (const t of open) byFile.set(t.path, (byFile.get(t.path) ?? 0) + 1);
  const topFiles = [...byFile.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6);
  const maxFile = topFiles[0]?.[1] ?? 1;

  return (
    <main className="page">
      <div className="page-head">
        <div className="grow">
          <h1>{open.length} open threads from {byReviewer.size} reviewer{byReviewer.size === 1 ? "" : "s"}</h1>
          <p className="muted">
            {pr.head_ref} → {pr.base_ref} · {pr.changed_files} files · fetched {new Date(pr.fetched_at).toLocaleTimeString()}{" "}
            <button className="link" onClick={() => ctx.refresh()}>Refresh</button>
          </p>
        </div>
        <a className="btn primary" href={prHref(pr.ref, "triage")}>Start triage</a>
      </div>

      <div className="cols">
        <section className="card grow-2">
          <div className="row-between">
            <h2>{aiName}'s suggested decisions</h2>
            <button className="btn sm" onClick={() => suggest.mutate()} disabled={suggest.isPending}>
              {suggest.isPending ? "Reading threads… (about a minute)" : hasSuggestions ? "Suggest again" : `Ask ${aiName} to suggest decisions`}
            </button>
          </div>
          {!hasSuggestions && !suggest.isPending && (
            <p className="muted small">
              One {aiName} call reads every open thread and suggests a decision for each. For stale threads it also
              compares the code the reviewer saw with the current code. Nothing is decided until you confirm it in Triage.
            </p>
          )}
          {suggest.error && <p className="error small">{(suggest.error as Error).message}</p>}
          {result && !suggest.isPending && <p className="result-line small">{result}</p>}
          {groups.map((g) => (
            <div key={g.key} className="group-block">
              <div className="bucket">
                <div className="bucket-n">{g.threads.length}</div>
                <div className="grow">
                  <div className="strong">{g.label}</div>
                  <div className="muted small">{g.detail}</div>
                </div>
                <a className="btn sm" href={prHref(pr.ref, "triage", encodeURIComponent(g.threads[0].id))}>
                  {g.key === "stale" ? "Review stale" : "Triage these"}
                </a>
              </div>
              <ThreadRows ctx={ctx} threads={g.threads} />
            </div>
          ))}
        </section>

        <div className="stack grow-1">
          <BranchStatusCard prRef={pr.ref} />
          <section className="card">
            <h2>Reviewers</h2>
            {[...byReviewer.entries()].sort((a, b) => b[1] - a[1]).map(([login, n]) => (
              <div key={login} className="reviewer">
                <span className="avatar">{login[0]?.toUpperCase()}</span>
                <div className="grow">
                  <div className="strong">{login}</div>
                  <div className="muted small">{(reviewerState.get(login) ?? "COMMENTED").replace("_", " ").toLowerCase()}</div>
                </div>
                <span className="small"><strong>{n}</strong> open</span>
              </div>
            ))}
          </section>
          <section className="card">
            <h2>Files with the most threads</h2>
            {topFiles.map(([path, n]) => (
              <div key={path} className="file-bar">
                <span className="mono small truncate" title={path}>{path.split("/").slice(-2).join("/")}</span>
                <span className="bar"><span style={{ width: `${(n / maxFile) * 100}%` }} /></span>
                <span className="small">{n}</span>
              </div>
            ))}
          </section>
        </div>
      </div>
    </main>
  );
}


const SHOW = 5;

/** The group's threads, each opening in Triage. */
function ThreadRows({ ctx, threads }: { ctx: PRContext; threads: Thread[] }) {
  const { pr, state } = ctx.data!;
  const [all, setAll] = useState(false);
  const shown = all ? threads : threads.slice(0, SHOW);
  return (
    <ul className="overview-threads">
      {shown.map((t) => {
        const d = state.decisions[t.id];
        const s = state.suggestions[t.id];
        return (
          <li key={t.id}>
            <a href={prHref(pr.ref, "triage", encodeURIComponent(t.id))} className="overview-thread">
              <span className="mark" aria-hidden="true">{d ? "✓" : ""}</span>
              <span className="grow min0">
                <span className="block truncate">{threadTitle(t, state)}</span>
                <span className="muted tiny">
                  <span className="mono">{where(t)}</span>
                  {d ? ` · decided: ${decisionLabel(d.decision)}` : s ? ` · ${s.reason}` : ` · ${t.comments[0]?.author}: ${excerpt(t.comments[0]?.body ?? "", 100)}`}
                </span>
              </span>
            </a>
          </li>
        );
      })}
      {threads.length > SHOW && (
        <li><button className="link small" onClick={() => setAll(!all)}>{all ? "Show fewer" : `Show all ${threads.length}`}</button></li>
      )}
    </ul>
  );
}
