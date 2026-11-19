/** Small colored marks for the two AI providers. Drawn for reviewr, not the vendors' logos. */

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

/** Codex: a green-to-blue tile with a terminal prompt. */
export function CodexIcon({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <defs>
        <linearGradient id="codex-g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#22C55E" />
          <stop offset="0.55" stopColor="#14B8A6" />
          <stop offset="1" stopColor="#3B82F6" />
        </linearGradient>
      </defs>
      <rect x="1.5" y="1.5" width="21" height="21" rx="6" fill="url(#codex-g)" />
      <path d="M7 8.5l3.5 3.5L7 15.5" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      <path d="M12.5 15.5h4.5" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  );
}

export function ProviderIcon({ provider, size }: { provider: "claude" | "codex"; size?: number }) {
  return provider === "claude" ? <ClaudeIcon size={size} /> : <CodexIcon size={size} />;
}
