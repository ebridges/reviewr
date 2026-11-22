import type { EditsConflict } from "../api";
import { CopyField } from "./CopyField";

/** After a patch is committed over files you had edited, your edits are merged back on
 *  top. This lists the files where they conflicted and still have conflict markers. */
export function EditConflicts({ files }: { files: string[] | undefined }) {
  if (!files?.length) return null;
  return (
    <p className="small warn-text">
      Your uncommitted edits were put back after the commit, but these files conflict with it and have conflict markers
      (<span className="mono">&lt;&lt;&lt;&lt;&lt;&lt;&lt; your edits</span>) to resolve:{" "}
      {files.map((f, i) => <span key={f}>{i > 0 && ", "}<span className="mono">{f}</span></span>)}.
    </p>
  );
}

/** Shown before committing a fix whose files you're also editing, when your edits
 *  wouldn't merge back cleanly. Nothing has changed yet; you choose whether to go ahead. */
export function EditsConflictPrompt({ conflict, onProceed, onCancel, busy }: {
  conflict: EditsConflict;
  onProceed: () => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const files = Object.entries(conflict.files);
  return (
    <div className="warn-box stack-gap small">
      <p>
        <strong>Your uncommitted edits conflict with this fix</strong> in{" "}
        {files.map(([f], i) => <span key={f}>{i > 0 && ", "}<span className="mono">{f}</span></span>)}. Nothing has been
        changed yet.
      </p>
      <p>
        If you go ahead, the fix is committed on its own and your edits are put back on top, with conflict markers
        (<span className="mono">&lt;&lt;&lt;&lt;&lt;&lt;&lt; your edits</span>) where the two overlap, for you to resolve. Or stop
        here, commit or stash your edits, and apply the fix after.
      </p>
      {files.map(([f, excerpt]) => (
        <details key={f}>
          <summary>What would conflict in <span className="mono">{f}</span></summary>
          <pre className="code"><div className="meta pre-wrap">{excerpt}</div></pre>
        </details>
      ))}
      <div className="row-wrap">
        <span className="muted">See your edits:</span>
        <CopyField value={`git -C ${conflict.worktree} status`} label="Command" />
      </div>
      <div className="row-wrap">
        <button className="btn primary sm" onClick={onProceed} disabled={busy}>
          {busy ? "Committing…" : `Commit the fix anyway (conflict markers in ${files.length} file${files.length === 1 ? "" : "s"})`}
        </button>
        <button className="btn sm" onClick={onCancel} disabled={busy}>Not now, I'll commit or stash first</button>
      </div>
    </div>
  );
}
