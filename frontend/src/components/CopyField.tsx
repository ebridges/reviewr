import { useState } from "react";

/** A read-only field with a copy button, for paths and commands. */
export function CopyField({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* the text is selectable in the field */
    }
  };
  return (
    <span className="copy-field">
      <input className="copy-field-input mono" value={value} readOnly aria-label={label} title={value}
        size={Math.min(Math.max(value.length, 20), 80)} onFocus={(e) => e.target.select()} />
      <button className="copy-field-btn" onClick={copy} aria-label={copied ? "Copied" : `Copy ${label.toLowerCase()}`} title={copied ? "Copied" : "Copy"}>
        {copied ? (
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
        ) : (
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round">
            <rect x="5.5" y="5.5" width="8" height="8" rx="1.5" />
            <path d="M10.5 3.5v-.5a1.5 1.5 0 0 0-1.5-1.5H3.5A1.5 1.5 0 0 0 2 3v5.5A1.5 1.5 0 0 0 3.5 10h.5" />
          </svg>
        )}
      </button>
    </span>
  );
}
