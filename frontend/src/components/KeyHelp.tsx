import { useEffect, useRef, useState, type ReactNode } from "react";

/** A "?" button that opens a small popup listing keyboard shortcuts. Also opens with
 *  the ? key; Esc or a click outside closes it. */
export function KeyHelp({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.tagName === "TEXTAREA" || t.tagName === "INPUT" || t.tagName === "SELECT") return;
      if (e.key === "?") { setOpen((o) => !o); e.preventDefault(); }
      else if (e.key === "Escape") setOpen(false);
    };
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDown);
    };
  }, []);

  return (
    <div className="key-help" ref={ref}>
      <button className="help-btn" aria-label="Keyboard shortcuts" aria-expanded={open} title="Keyboard shortcuts (?)"
        onClick={() => setOpen(!open)}>
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8"
          strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="10" />
          <path d="M9.1 9a3 3 0 0 1 5.8 1c0 2-3 2.5-3 4.5" />
          <circle cx="12" cy="17.6" r="0.6" fill="currentColor" />
        </svg>
      </button>
      {open && (
        <div className="help-pop" role="dialog" aria-label="Keyboard shortcuts">
          <div className="label">Keyboard shortcuts</div>
          <div className="muted small keys">
            {children}
            <div><span className="kbd">?</span> show or hide this</div>
          </div>
        </div>
      )}
    </div>
  );
}
