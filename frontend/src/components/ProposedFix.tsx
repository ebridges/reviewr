import type { ReactNode } from "react";
import { DiffView } from "./Code";
import { Markdown } from "./Markdown";

export interface ProposedFixData {
  status: "running" | "ready" | "failed" | "applied";
  failure: string;
  label: string;
  ai_name: string;
  diff: string;
  summary: string;
  error: string | null;
}

/** "A and B": the names of the failed checks, for a title. */
export function failedLabel(names: string[]): string {
  return names.length <= 1 ? names[0] ?? "" : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/** What a fix is for. Fixes proposed before labels were stored get one from the failure text. */
export function forWhat(fix: ProposedFixData): string {
  if (fix.label) return fix.label;
  if (/commit hooks failed/.test(fix.failure)) return "commit hooks";
  const names = [...fix.failure.matchAll(/(?:^|\n\n)(.+?) (?:failed|couldn't run)\b/g)].map((m) => m[1].trim());
  return failedLabel(names);
}

/** The agent's proposed change for failing checks: its explanation, the diff, and
 *  Apply / Discard. Shared by the Fix page and the merge's Finish page. */
export function ProposedFix({
  fix, where, onWhat, applyLabel, applyNote, onApply, onDiscard, onRetry, busy, error, target,
}: {
  fix: ProposedFixData;
  where: string; // "the merged code", "your changes"
  onWhat: string; // "on top of your resolutions"
  applyLabel: string;
  applyNote: string;
  onApply: () => void;
  onDiscard: () => void;
  onRetry: () => void;
  busy: boolean;
  error?: Error | null;
  target?: ReactNode; // where applying writes, e.g. <CheckoutPath />
}) {
  const files = new Set([...fix.diff.matchAll(/^diff --git a\/.+? b\/(.+)$/gm)].map((m) => m[1]));
  const n = files.size;
  return (
    <section className="card stack-gap fix-card">
      <h2>Proposed fix{forWhat(fix) ? ` for ${forWhat(fix)}` : ""}</h2>
      {fix.status === "running" && (
        <p className="small">{fix.ai_name} is reading the failure and changing {where}. This usually takes a few minutes.</p>
      )}
      {fix.status === "failed" && (
        <>
          <p className="error small">{fix.error}</p>
          <div className="row-wrap">
            <button className="btn sm" onClick={onRetry} disabled={busy}>Try again</button>
            <button className="link small" onClick={onDiscard} disabled={busy}>Dismiss</button>
          </div>
        </>
      )}
      {fix.status === "ready" && (
        <>
          {fix.summary && (
            <div className="suggest"><strong className="small">{fix.ai_name}</strong><div className="small"><Markdown text={fix.summary} /></div></div>
          )}
          {fix.diff.trim() ? (
            <>
              <p className="muted small">Changes to {n} file{n === 1 ? "" : "s"} {onWhat}. {applyNote}</p>
              <DiffView text={fix.diff} maxLines={800} />
              {target}
            </>
          ) : (
            <p className="small">{fix.ai_name} didn't change any files.</p>
          )}
          <div className="row-wrap">
            {fix.diff.trim() && <button className="btn primary sm" onClick={onApply} disabled={busy}>{applyLabel}</button>}
            <button className="btn sm" onClick={onDiscard} disabled={busy}>Discard</button>
          </div>
        </>
      )}
      <details>
        <summary className="tiny">The failure that was sent</summary>
        <pre className="code"><div className="meta pre-wrap">{fix.failure}</div></pre>
      </details>
      {error && <p className="error small">{error.message}</p>}
    </section>
  );
}
