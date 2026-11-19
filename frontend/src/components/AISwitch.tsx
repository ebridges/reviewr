import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { ProviderIcon } from "./AIIcons";

const OPTIONS = [
  { id: "claude", label: "Claude", missing: "not connected" },
  { id: "codex", label: "Codex", missing: "not signed in" },
] as const;

/** Which AI does the work (suggestions, replies, planning, checks, the coding agent). */
export function AISwitch() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["ai"], queryFn: api.ai, staleTime: 60_000 });
  const change = useMutation({ mutationFn: api.setAI, onSuccess: (d) => qc.setQueryData(["ai"], d) });
  const d = status.data;
  if (!d) return null;
  return (
    <div className="ai-switch small" role="radiogroup" aria-label="AI provider"
      title={change.error ? (change.error as Error).message : "AI used for suggestions, replies, planning, checks and the coding agent"}>
      {OPTIONS.map((o) => {
        const ready = o.id === "claude" ? d.claude_ready : d.codex_ready;
        const on = d.provider === o.id;
        return (
          <button key={o.id} role="radio" aria-checked={on} className={`ai-opt${on ? " on" : ""}`}
            disabled={!ready || change.isPending}
            title={ready ? `Use ${o.label}` : `${o.label} is ${o.missing}; see Settings`}
            onClick={() => !on && change.mutate(o.id)}>
            <ProviderIcon provider={o.id} />
            {o.label}
          </button>
        );
      })}
    </div>
  );
}
