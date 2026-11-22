/** Marks for the two AI providers: a drawn spark for Claude, OpenAI's favicon for Codex. */

/** Claude: an orange eight-point spark. */
export function ClaudeIcon({ size = 16 }: { size?: number }) {
  const rays = [0, 45, 90, 135];
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <defs>
        {/* userSpaceOnUse: a straight line has a zero-width bounding box, so bbox units draw nothing */}
        <linearGradient id="claude-g" gradientUnits="userSpaceOnUse" x1="2" y1="2" x2="22" y2="22">
          <stop offset="0" stopColor="#F4A46A" />
          <stop offset="1" stopColor="#D2582F" />
        </linearGradient>
      </defs>
      <g stroke="url(#claude-g)" strokeLinecap="round">
        {rays.map((a) => (
          <line key={a} x1="12" y1="2.5" x2="12" y2="21.5" strokeWidth={a % 90 ? 2.6 : 3.4} transform={`rotate(${a} 12 12)`} />
        ))}
      </g>
      <circle cx="12" cy="12" r="3.2" fill="#D2582F" />
    </svg>
  );
}

/** Codex: OpenAI's favicon. */
export function CodexIcon({ size = 16 }: { size?: number }) {
  return <img src="/brands/openai.svg" alt="" width={size} height={size} aria-hidden="true" style={{ display: "block" }} />;
}

export function ProviderIcon({ provider, size }: { provider: "claude" | "codex"; size?: number }) {
  return provider === "claude" ? <ClaudeIcon size={size} /> : <CodexIcon size={size} />;
}
