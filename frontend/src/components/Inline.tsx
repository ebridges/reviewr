/** Short AI-written text with `code` spans rendered as code, without full Markdown. */
export function Inline({ text }: { text: string }) {
  const parts = text.split(/(`[^`\n]+`)/g);
  return <>{parts.map((p, i) => (p.startsWith("`") && p.endsWith("`") && p.length > 2 ? <code key={i}>{p.slice(1, -1)}</code> : p))}</>;
}
