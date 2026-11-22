/** A small rotating ring: an agent or a job is working. */
export function Spinner({ size = 12, label = "Working" }: { size?: number; label?: string }) {
  return <span className="spinner" style={{ width: size, height: size }} role="status" aria-label={label} />;
}
