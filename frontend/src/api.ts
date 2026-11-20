// Types mirror backend/reviewr/models.py, state.py and submit.py.
import type { ProposedFixData } from "./components/ProposedFix";

export interface PRRef { owner: string; repo: string; number: number }

export interface Comment { id: string; author: string; body: string; created_at: string; url: string }

export interface Thread {
  id: string;
  path: string;
  line: number | null;
  original_line: number | null;
  start_line: number | null;
  is_resolved: boolean;
  is_outdated: boolean; // shown as "stale"
  original_commit: string | null;
  diff_hunk: string;
  comments: Comment[];
}

export interface ReviewerStatus { login: string; user_id: string | null; state: string }

export interface PullRequest {
  ref: PRRef;
  title: string;
  url: string;
  author: string;
  base_ref: string;
  head_ref: string;
  head_oid: string;
  changed_files: number;
  threads: Thread[];
  reviewers: ReviewerStatus[];
  fetched_at: string;
}

export interface PRSummary {
  ref: PRRef;
  node_id: string;
  title: string;
  url: string;
  is_draft: boolean;
  updated_at: string;
  head_ref: string;
  changed_files: number;
  review_decision: string | null;
  open_threads: number;
  ci: string | null;
}

export type Decision = "fix" | "reply" | "push_back" | "already_done" | "defer" | "no_action";
export type StaleVerdict = "addressed" | "not_addressed" | "unclear";

export interface Suggestion {
  thread_id: string;
  title: string;
  decision: Decision;
  reason: string;
  similar: string[];
  stale_verdict: StaleVerdict | null;
}

export interface ThreadDecision { decision: Decision; note: string; reply: string; resolve: boolean; decided_at: string }

export interface ThreadCheck { addressed: boolean; note: string }

export interface WorkItem {
  id: number;
  title: string;
  thread_ids: string[];
  mode: "agent" | "me";
  status: "pending" | "drafting" | "ready" | "failed" | "committed";
  patch: string;
  checks: Record<string, ThreadCheck>;
  commit_sha: string | null;
  error: string | null;
  agent_log: string;
  instruction: string;
  prompt: string | null;
  check_report: CheckReport | null;
  checks_running: boolean;
  proposed_fix: ProposedFixData | null;
}

export interface CheckRun {
  name: string;
  kind: "lint" | "test";
  status: "passed" | "failed" | "skipped" | "error";
  reason: string;
  command: string;
  files: string[];
  tests: string[];
  output: string;
  duration_s: number;
}

export interface CheckReport { ran_at: string; scope: string[]; runs: CheckRun[] }

export interface JiraIssue { key: string; url: string; thread_ids: string[] }

export interface CleanupItem { path: string; label?: string; size_mb?: number; reason: string; pr?: string }
export interface CleanupView {
  plan: {
    envs_remove: CleanupItem[];
    envs_keep: CleanupItem[];
    worktrees_remove: CleanupItem[];
    worktrees_keep: CleanupItem[];
    open_prs: string[];
    unknown_prs: string[];
  };
  remove_mb: number;
  keep_mb: number;
}

export interface AIStatus { provider: "claude" | "codex"; claude_ready: boolean; codex_ready: boolean; codex_status: string | null }

export interface WorkStatus {
  path: string | null;
  branch: string | null;
  expected_branch?: string;
  created_by_reviewr: boolean;
  ahead?: number;
  behind?: number;
  uncommitted?: string[];
}

export interface EnvStatus { name: string; key: string; status: "ready" | "missing" | "installing" | "failed"; path: string; log_tail: string }

export interface ReviewState {
  suggestions: Record<string, Suggestion>;
  decisions: Record<string, ThreadDecision>;
  plan: WorkItem[];
  jira_issues: JiraIssue[];
  summary: string | null;
  submitted_at: string | null;
}

export interface PRView { pr: PullRequest; state: ReviewState }

export interface StaleTrace {
  status: "unchanged" | "moved" | "edited" | "deleted" | "unknown";
  original_commit: string | null;
  original_path: string;
  original_line: number | null;
  current_path: string | null;
  current_line: number | null;
  commits: { sha: string; subject: string }[];
  original_snippet: string;
  current_snippet: string;
  snippet_start_original: number;
  snippet_start_current: number;
}

export interface Submission {
  branch: string;
  commits_to_push: number;
  summary: string;
  replies: { thread_id: string; decision: string; body: string; resolve: boolean }[];
  missing_replies: string[];
  rerequest: string[];
}

export interface Settings {
  anthropic: string | null;
  codex: { installed: boolean; status: string | null };
  github: string | null;
  github_login: string | null;
  rate_limit: { cost: number; remaining: number; reset_at: string } | null;
  jira: { configured: boolean; base_url?: string; project?: string; issue_type?: string; user?: string; error?: string };
  model: string;
  repos: Record<string, { path: string; checks: string[]; exclude_tests: string[]; envs: string[] }>;
  editors: string[];
  default_editor: string;
}

export interface AppConfig { editors: string[]; default_editor: string; model: string; jira_project: string | null }

export function parsePrRef(text: string): PRRef | null {
  const m =
    text.match(/github\.com\/([^/]+)\/([^/]+)\/pull\/(\d+)/) ??
    text.trim().match(/^([^/\s]+)\/([^#\s]+)#(\d+)$/);
  return m ? { owner: m[1], repo: m[2], number: Number(m[3]) } : null;
}

// Update from main (backend/reviewr/merge.py)
export type ConflictCategory = "auto" | "input" | "propose";
export type ConflictOption = "ours" | "theirs" | "ours_then_theirs" | "theirs_then_ours" | "proposal" | "custom" | "delete";
export type ConflictKind = "text" | "deleted_in_pr" | "deleted_in_main" | "binary" | "lockfile";

export interface MergeCommit { sha: string; author: string; subject: string }

export interface Conflict {
  id: string;
  path: string;
  index: number;
  kind: ConflictKind;
  ours: string | null;
  base: string | null;
  theirs: string | null;
  before: string;
  after: string;
  start_line: number | null;
  category: ConflictCategory | null;
  title: string;
  reason: string;
  recommended: ConflictOption | null;
  proposal: string | null;
  source: "rule" | "ai" | null;
  ai_name: string | null;
  choice: ConflictOption | null;
  custom: string | null;
  resolved: boolean;
}

export interface MergeFile {
  path: string;
  kind: ConflictKind;
  conflict_ids: string[];
  pr_commits: MergeCommit[];
  main_commits: MergeCommit[];
}

export interface MergeFix {
  status: "running" | "ready" | "failed" | "applied";
  failure: string;
  label: string;
  ai_name: string;
  diff: string;
  summary: string;
  error: string | null;
}

export interface MergeSession {
  base_ref: string;
  base_sha: string;
  head_sha: string;
  head_ref: string;
  started_at: string;
  status: "analyzing" | "ready" | "committed" | "failed";
  error: string | null;
  files: MergeFile[];
  conflicts: Record<string, Conflict>;
  clean_files: string[];
  commits: MergeCommit[];
  commit_count: number;
  commit_sha: string | null;
  hook_fixes: string[];
  pushed: boolean;
  check_report: CheckReport | null;
  checks_running: boolean;
  fix: MergeFix | null;
  fixed_paths: string[];
}

export interface MergeView { session: MergeSession | null; options: Record<string, ConflictOption[]> }

export interface RemoteStatus {
  ahead: number;
  behind: number;
  conflicted_files: string[];
  commits: MergeCommit[];
  fetch_error: string | null;
}

export interface MergePreview {
  base_ref: string;
  base_sha: string;
  head_sha: string;
  behind: number;
  fetch_error: string | null;
  conflicted_files: string[];
  changed_files: number;
  commits: MergeCommit[];
}

export const refKey = (r: PRRef) => `${r.owner}/${r.repo}#${r.number}`;
const base = (r: PRRef) => `/api/prs/${r.owner}/${r.repo}/${r.number}`;

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : body.detail ? JSON.stringify(body.detail) : "";
    throw new Error(detail || `The server returned ${res.status} ${res.statusText}. Check the backend log.`);
  }
  return res.json();
}

const send = (method: string, body?: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

export const api = {
  prs: (which: "mine" | "review") => call<PRSummary[]>(`/api/prs?which=${which}`),
  setStatus: (r: PRRef, nodeId: string, status: "draft" | "ready" | "closed") =>
    call<{ state: string; is_draft?: boolean }>(`${base(r)}/status`, send("POST", { node_id: nodeId, status })),
  pr: (r: PRRef, refresh = false) => call<PRView>(`${base(r)}?refresh=${refresh}`),
  suggest: (r: PRRef, force = false) => call<ReviewState>(`${base(r)}/suggest?force=${force}`, send("POST")),
  trace: (r: PRRef, threadId: string) => call<StaleTrace>(`${base(r)}/threads/${encodeURIComponent(threadId)}/trace`),
  decide: (r: PRRef, threadIds: string[], decision: Decision, note?: string) =>
    call<ReviewState>(`${base(r)}/decisions`, send("PUT", { thread_ids: threadIds, decision, note })),
  clearDecisions: (r: PRRef, threadIds: string[]) =>
    call<ReviewState>(`${base(r)}/decisions/clear`, send("POST", { thread_ids: threadIds })),
  undecide: (r: PRRef, threadId: string) =>
    call<ReviewState>(`${base(r)}/decisions/${encodeURIComponent(threadId)}`, send("DELETE")),
  patchDecision: (r: PRRef, threadId: string, patch: Partial<Pick<ThreadDecision, "note" | "reply" | "resolve">>) =>
    call<ReviewState>(`${base(r)}/decisions/${encodeURIComponent(threadId)}`, send("PATCH", patch)),
  draftReplies: (r: PRRef, threadIds?: string[]) =>
    call<ReviewState>(`${base(r)}/replies/draft`, send("POST", { thread_ids: threadIds ?? null })),
  plan: (r: PRRef) => call<ReviewState>(`${base(r)}/plan`, send("POST")),
  itemPrompt: (r: PRRef, id: number) => call<{ prompt: string; default: string; edited: boolean }>(`${base(r)}/plan/${id}/prompt`),
  patchItem: (r: PRRef, id: number, patch: { mode?: string; title?: string; instruction?: string; prompt?: string; reset_prompt?: boolean }) =>
    call<ReviewState>(`${base(r)}/plan/${id}`, send("PATCH", patch)),
  draftAll: (r: PRRef) => call<ReviewState>(`${base(r)}/plan/draft-all`, send("POST")),
  draft: (r: PRRef, id: number) => call<ReviewState>(`${base(r)}/plan/${id}/draft`, send("POST")),
  accept: (r: PRRef, id: number, message: string) =>
    call<ReviewState>(`${base(r)}/plan/${id}/accept`, send("POST", { message })),
  runChecks: (r: PRRef, id: number) => call<ReviewState>(`${base(r)}/plan/${id}/checks`, send("POST")),
  createJira: (r: PRRef, threadIds?: string[]) =>
    call<ReviewState>(`${base(r)}/jira`, send("POST", { thread_ids: threadIds ?? null })),
  config: () => call<AppConfig>("/api/config"),
  ai: () => call<AIStatus>("/api/ai"),
  setAI: (provider: "claude" | "codex") => call<AIStatus>("/api/ai", send("PUT", { provider })),
  cleanupPlan: () => call<CleanupView>("/api/cleanup"),
  cleanup: () => call<{ done: string[]; freed_mb: number }>("/api/cleanup", send("POST", { confirm: true })),
  envs: (r: PRRef) => call<EnvStatus[]>(`${base(r)}/envs`),
  installEnvs: (r: PRRef) => call<EnvStatus[]>(`${base(r)}/envs/install`, send("POST")),
  work: (r: PRRef) => call<WorkStatus>(`${base(r)}/work`),
  workDiff: (r: PRRef) => call<{ diff: string; worktree: string }>(`${base(r)}/work/diff`),
  ownFix: (r: PRRef, id: number, failure: string, label: string) =>
    call<ReviewState>(`${base(r)}/plan/${id}/fix`, send("POST", { failure, label })),
  ownFixApply: (r: PRRef, id: number) => call<ReviewState>(`${base(r)}/plan/${id}/fix/apply`, send("POST")),
  ownFixDiscard: (r: PRRef, id: number) => call<ReviewState>(`${base(r)}/plan/${id}/fix`, send("DELETE")),
  check: (r: PRRef, id: number) => call<ReviewState>(`${base(r)}/plan/${id}/check`, send("POST")),
  commitOwn: (r: PRRef, id: number, message: string, paths: string[]) =>
    call<ReviewState>(`${base(r)}/plan/${id}/commit`, send("POST", { message, paths })),
  submission: (r: PRRef) => call<Submission>(`${base(r)}/submission`),
  setSummary: (r: PRRef, summary: string) => call<ReviewState>(`${base(r)}/summary`, send("PUT", { summary })),
  submit: (r: PRRef) => call<{ done: string[] }>(`${base(r)}/submit`, send("POST", { confirm: true })),
  open: (r: PRRef, opts: { path?: string; line?: number | null; editor: string }) =>
    call<{ argv: string[] }>(`${base(r)}/open`, send("POST", opts)),
  settings: () => call<Settings>("/api/settings"),
  mergeRemote: (r: PRRef) => call<RemoteStatus>(`${base(r)}/merge/remote`),
  mergeRemoteSync: (r: PRRef) => call<RemoteStatus>(`${base(r)}/merge/remote/sync`, send("POST", { confirm: true })),
  mergePreview: (r: PRRef) => call<MergePreview>(`${base(r)}/merge/preview`),
  merge: (r: PRRef) => call<MergeView>(`${base(r)}/merge`),
  mergeStart: (r: PRRef) => call<MergeView>(`${base(r)}/merge/start`, send("POST")),
  mergeReanalyze: (r: PRRef) => call<MergeView>(`${base(r)}/merge/reanalyze`, send("POST")),
  mergePatch: (r: PRRef, id: string, patch: { category?: ConflictCategory; choice?: ConflictOption; custom?: string; resolved?: boolean }) =>
    call<MergeView>(`${base(r)}/merge/conflicts/${encodeURIComponent(id)}`, send("PATCH", patch)),
  mergeAccept: (r: PRRef, ids: string[]) => call<MergeView>(`${base(r)}/merge/accept`, send("POST", { conflict_ids: ids })),
  mergePropose: (r: PRRef, id: string, instruction: string) =>
    call<MergeView>(`${base(r)}/merge/conflicts/${encodeURIComponent(id)}/propose`, send("POST", { instruction })),
  mergeDiff: (r: PRRef, path: string) => call<{ diff: string }>(`${base(r)}/merge/diff?path=${encodeURIComponent(path)}`),
  mergeChecks: (r: PRRef) => call<MergeView>(`${base(r)}/merge/checks`, send("POST")),
  mergeFix: (r: PRRef, failure: string, label: string) => call<MergeView>(`${base(r)}/merge/fix`, send("POST", { failure, label })),
  mergeFixApply: (r: PRRef) => call<MergeView>(`${base(r)}/merge/fix/apply`, send("POST")),
  mergeFixDiscard: (r: PRRef) => call<MergeView>(`${base(r)}/merge/fix`, send("DELETE")),
  mergeCommit: (r: PRRef, message: string) => call<MergeView>(`${base(r)}/merge/commit`, send("POST", { message })),
  mergePush: (r: PRRef) => call<MergeView>(`${base(r)}/merge/push`, send("POST", { confirm: true })),
  mergeDiscard: (r: PRRef) => call<MergeView>(`${base(r)}/merge`, send("DELETE")),
};
