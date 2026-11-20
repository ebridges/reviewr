import { useState } from "react";
import { storageGet, storageSet } from "../lib";

/** Which groups are folded in one place (Overview, Triage's panel, Triage's list),
 *  remembered across visits. */
export function useFolded(scope: string) {
  const key = `reviewr:folded:${scope}`;
  const [folded, setFolded] = useState<Set<string>>(() => {
    try { return new Set(JSON.parse(storageGet(key) ?? "[]") as string[]); } catch { return new Set(); }
  });
  const toggle = (group: string) => {
    const next = new Set(folded);
    if (next.has(group)) next.delete(group); else next.add(group);
    setFolded(next);
    storageSet(key, JSON.stringify([...next]));
  };
  return { isFolded: (group: string) => folded.has(group), toggle };
}

/** The chevron that folds a group. */
export function FoldButton({ folded, onClick, label }: { folded: boolean; onClick: () => void; label: string }) {
  return (
    <button className={`fold-btn ${folded ? "folded" : ""}`} onClick={onClick} aria-expanded={!folded}
      aria-label={`${folded ? "Show" : "Hide"} ${label}`} title={folded ? "Show" : "Hide"}>
      <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path d="M4 6l4 4 4-4" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
    </button>
  );
}

/** The category's color as a dot. `kind` is a decision, "stale" or "none". */
export function Dot({ kind }: { kind: string }) {
  return <span className={`dc-dot dc-${kind}`} aria-hidden="true" />;
}
