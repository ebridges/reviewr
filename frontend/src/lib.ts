import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, type Decision, type PRRef, type PRView, type ReviewState, type Thread } from "./api";

export const DECISIONS: { key: Decision; label: string; shortcut: string }[] = [
  { key: "fix", label: "Fix", shortcut: "f" },
  { key: "reply", label: "Reply only", shortcut: "r" },
  { key: "push_back", label: "Push back", shortcut: "p" },
  { key: "already_done", label: "Already done", shortcut: "a" },
  { key: "defer", label: "Defer", shortcut: "d" },
  { key: "no_action", label: "No action", shortcut: "n" },
];

export const decisionLabel = (d: Decision) => DECISIONS.find((x) => x.key === d)?.label ?? d;

export const GROUP_LABEL: Record<Decision, string> = {
  fix: "Code change requested",
  reply: "Questions",
  push_back: "Push back",
  already_done: "Already done",
  defer: "Out of scope",
  no_action: "No action needed",
};

// Routing: #/  #/settings  #/pr/owner/repo/123/triage[/<item>]  #/pr/owner/repo/123/merge[/resolve/<id>]
export interface Route {
  page: "prs" | "settings" | "overview" | "triage" | "plan" | "fix" | "respond" | "merge";
  ref?: PRRef;
  arg?: string;
}

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  if (parts[0] === "settings") return { page: "settings" };
  if (parts[0] === "pr" && parts.length >= 4) {
    const ref = { owner: parts[1], repo: parts[2], number: Number(parts[3]) };
    const page = (parts[4] ?? "overview") as Route["page"];
    return { page, ref, arg: parts.slice(5).join("/") || undefined };
  }
  return { page: "prs" };
}

export const prHref = (r: PRRef, page = "overview", arg?: string) =>
  `#/pr/${r.owner}/${r.repo}/${r.number}/${page}${arg ? `/${arg}` : ""}`;

export function useRoute(): Route {
  const [hash, setHash] = useState(location.hash);
  useEffect(() => {
    const on = () => setHash(location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return parseRoute(hash);
}

/** The PR and its review state, with a helper to apply state returned by a mutation.
 *  Polls every 3 seconds while an agent is drafting, to pick up its result. */
export function usePR(ref: PRRef) {
  const qc = useQueryClient();
  const key = ["pr", ref.owner, ref.repo, ref.number];
  const q = useQuery({
    queryKey: key,
    queryFn: () => api.pr(ref),
    refetchInterval: (query) =>
      query.state.data?.state.plan.some((w) => w.status === "drafting" || w.checks_running) ? 3000 : false,
  });
  const setState = (state: ReviewState) =>
    qc.setQueryData<PRView>(key, (old) => (old ? { ...old, state } : old));
  const refresh = async () => qc.setQueryData(key, await api.pr(ref, true));
  return { ...q, setState, refresh };
}

/** Markdown and inline HTML reduced to readable plain text, for titles and excerpts.
 *  Image badges keep their label: `![P1 Badge](…)` becomes "P1 ·". */
export function plainText(md: string): string {
  return md
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/!\[([^\]]*?)(?: badge)?\]\([^)]*\)/gi, (_, alt: string) => (alt ? `${alt} · ` : ""))
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/<\/?[a-z][^>]*>/gi, "")
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+/gm, "")
    .replace(/(\*\*|__|\*|_|`|~~)/g, "")
    .replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/[ \t]+/g, " ")
    .trim();
}

export function excerpt(md: string, max = 160): string {
  const text = plainText(md).replace(/\s+/g, " ");
  return text.length > max ? text.slice(0, max - 1) + "…" : text;
}

export function threadTitle(t: Thread, state?: ReviewState): string {
  const s = state?.suggestions[t.id];
  if (s) return s.title;
  const line = plainText(t.comments[0]?.body ?? "").split("\n").find((l) => l.trim()) ?? "(empty comment)";
  return excerpt(line, 90);
}

/** What happens to a thread after each decision, shown under the decision buttons. */
export const DECISION_HELP: Record<Decision, string> = {
  fix: "Goes into a work item in Plan. The commit that fixes it is linked in your reply, and the thread is resolved.",
  reply: "No code change. {ai} drafts an answer from the code and your note. It's posted in Respond and the thread stays open.",
  push_back: "No code change. Say why the code should stay as it is; {ai} turns that into a short reply. It's posted in Respond and the thread stays open for the reviewer.",
  already_done: "No code change. Your reply points at the change that already addressed it, and the thread is resolved.",
  defer: "No code change. Filed in Jira from Plan, and your reply names the issue. The thread stays open.",
  no_action: "Nothing to change and nothing to say, such as your own notes for reviewers. No reply is posted and the thread is left as it is.",
};

export const where = (t: Thread) => `${t.path}:${t.line ?? t.original_line ?? "?"}`;

export function storageGet(key: string): string | null {
  try { return localStorage.getItem(key); } catch { return null; }
}
export function storageSet(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* storage unavailable */ }
}

/** "Claude" or "Codex": whichever AI is selected in the header. Shares the header
 *  switch's query, so labels change as soon as you switch. */
export function useAIName(): string {
  const q = useQuery({ queryKey: ["ai"], queryFn: api.ai, staleTime: 60_000 });
  return q.data?.provider === "codex" ? "Codex" : "Claude";
}
