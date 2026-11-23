import { useQuery } from "@tanstack/react-query";
import { api, type CISession, type MergeSession, type PRView } from "../api";
import { prHref, useAIName } from "../lib";
import { ciKey } from "../pages/CI";
import { mergeKey } from "../pages/Merge";
import { useRun } from "../pages/Run";
import { Spinner } from "./Spinner";

const mergeBusy = (s?: MergeSession | null) => !!s && (s.status === "analyzing" || s.checks_running || s.fix?.status === "running");
const ciBusy = (s?: CISession | null) => !!s && (s.status === "analyzing" || Object.values(s.failures).some((f) => f.fix?.status === "running"));

/** What's running in the background for this PR, in any of the three workflows, with a
 *  link to each. Shown at the bottom of the header on every page of the PR. */
export function WorkingBar({ view }: { view: PRView }) {
  const ref = view.pr.ref;
  const ai = useAIName();
  // Same query keys as the workflow pages; these poll only while something is running.
  const merge = useQuery({
    queryKey: mergeKey(ref), queryFn: () => api.merge(ref), retry: false,
    refetchInterval: (q) => (mergeBusy(q.state.data?.session) ? 3000 : false),
  });
  const ci = useQuery({
    queryKey: ciKey(ref), queryFn: () => api.ci(ref), retry: false,
    refetchInterval: (q) => (ciBusy(q.state.data?.session) ? 3000 : false),
  });

  const tasks: { label: string; href: string }[] = [];
  for (const w of view.state.plan) {
    const href = prHref(ref, "fix", String(w.id));
    if (w.status === "drafting" && !w.checks_running) tasks.push({ label: `${ai} is drafting work item ${w.id}`, href });
    if (w.checks_running) tasks.push({ label: `Running checks on work item ${w.id}`, href });
    if (w.proposed_fix?.status === "running") tasks.push({ label: `${w.proposed_fix.ai_name} is fixing checks on work item ${w.id}`, href });
  }
  const m = merge.data?.session;
  if (m?.status === "analyzing") tasks.push({ label: `${ai} is triaging merge conflicts`, href: prHref(ref, "merge", "triage") });
  if (m?.checks_running) tasks.push({ label: "Running checks on the merge", href: prHref(ref, "merge", "finish") });
  if (m?.fix?.status === "running") tasks.push({ label: `${m.fix.ai_name} is fixing ${m.fix.label || "the merge"}`, href: prHref(ref, "merge", "finish") });
  const c = ci.data?.session;
  if (c?.status === "analyzing") tasks.push({ label: `${ai} is analyzing CI failures`, href: prHref(ref, "ci", "triage") });
  for (const f of Object.values(c?.failures ?? {})) {
    if (f.fix?.status === "running") tasks.push({ label: `${f.fix.ai_name} is fixing CI: ${f.title || f.name}`, href: prHref(ref, "ci", `fix/${encodeURIComponent(f.id)}`) });
  }
  const r = useRun(ref).data?.session;
  if (r?.status === "starting") tasks.push({ label: "Starting the app", href: prHref(ref, "run") });
  if (r?.status === "stopping") tasks.push({ label: "Stopping the app", href: prHref(ref, "run") });
  if (!tasks.length) return null;
  return (
    <div className="working-bar" role="status" aria-live="polite">
      <Spinner size={13} />
      {tasks.map((t, i) => (
        <span key={t.label}>
          {i > 0 && <span className="muted"> · </span>}
          <a href={t.href}>{t.label}</a>
        </span>
      ))}
    </div>
  );
}
