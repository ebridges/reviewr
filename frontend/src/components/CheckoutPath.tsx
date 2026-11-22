import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type PRRef } from "../api";
import { storageGet } from "../lib";
import { CopyField } from "./CopyField";
import { OpenInEditor } from "./Brand";

/** Where your checkout of the PR branch is, with Copy and Open: shown wherever reviewr
 *  writes to it, so you can go there when something needs a hand. */
export function CheckoutPath({ prRef, label = "Your checkout" }: { prRef: PRRef; label?: string }) {
  const work = useQuery({ queryKey: ["work", prRef.number], queryFn: () => api.work(prRef) });
  const editor = storageGet("reviewr:editor") || "vscode";
  const [note, setNote] = useState<string>();
  const path = work.data?.path;
  if (!path) return null;
  const open = async () => {
    try {
      await api.open(prRef, { editor });
    } catch (e) {
      setNote((e as Error).message);
    }
  };
  return (
    <div className="checkout-path small">
      <span className="muted">{label}:</span>
      <CopyField value={path} label="Checkout path" />
      {work.data?.branch && <span className="muted">{work.data.branch}</span>}
      <OpenInEditor editor={editor} onClick={open} />
      {note && <span className="muted tiny">{note}</span>}
    </div>
  );
}
