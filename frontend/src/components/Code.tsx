/** Unified diff text with +/- lines colored. */
export function DiffView({ text, maxLines = 400 }: { text: string; maxLines?: number }) {
  const lines = text.split("\n");
  const shown = lines.slice(0, maxLines);
  return (
    <pre className="code">
      {shown.map((l, i) => {
        const cls = l.startsWith("+++") || l.startsWith("---") || l.startsWith("diff ") || l.startsWith("index ")
          ? "meta"
          : l.startsWith("@@") ? "hdr" : l.startsWith("+") ? "add" : l.startsWith("-") ? "del" : "";
        return <div key={i} className={cls}>{l || " "}</div>;
      })}
      {lines.length > maxLines && <div className="meta">… {lines.length - maxLines} more lines</div>}
    </pre>
  );
}

/** A plain code excerpt with line numbers, highlighting one line. */
export function Snippet({ text, start, highlight }: { text: string; start: number; highlight?: number | null }) {
  if (!text) return <pre className="code"><div className="meta">(not available)</div></pre>;
  return (
    <pre className="code">
      {text.split("\n").map((l, i) => (
        <div key={i} className={start + i === highlight ? "hl" : ""}>
          <span className="ln">{start + i}</span>{l || " "}
        </div>
      ))}
    </pre>
  );
}
