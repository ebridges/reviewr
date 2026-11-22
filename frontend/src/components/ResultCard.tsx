import { useQuery } from "@tanstack/react-query";
import { Fragment, type ReactNode } from "react";
import { api, type PRRef } from "../api";
import { prHref } from "../lib";
import { CopyField } from "./CopyField";

export interface Detail { label: string; value: string | null | undefined }

/** The confirmation screen used when something finishes (a merge, a commit, a push, a
 *  submitted review): a title, a colored status pill, one sentence on what happened,
 *  anything still to do, then the details with copy buttons, and Back to overview. */
export function ResultCard({
  prRef, title, status, tone = "ok", summary, details, children, actions,
}: {
  prRef: PRRef;
  title: string;
  status: string;
  tone?: "ok" | "warn" | "info";
  summary: ReactNode;
  details: Detail[];
  children?: ReactNode;
  actions?: ReactNode;
}) {
  const shown = details.filter((d) => d.value);
  return (
    <section className="card stack-gap result-card">
      <h1>{title}</h1>
      <div><span className={`pill status-pill ${tone}`}>{status}</span></div>
      <p>{summary}</p>
      {children}
      {shown.length > 0 && (
        <div className="stack-gap">
          <h2 className="h3">Details</h2>
          <dl className="details">
            {shown.map((d) => (
              <Fragment key={d.label}>
                <dt className="muted small">{d.label}</dt>
                <dd><CopyField value={d.value!} label={d.label} /></dd>
              </Fragment>
            ))}
          </dl>
        </div>
      )}
      <div className="row-wrap">
        <a className="btn" href={prHref(prRef)}>Back to overview</a>
        {actions}
      </div>
    </section>
  );
}

/** Clone directory and PR branch, for a ResultCard's details. */
export function useCheckoutDetails(prRef: PRRef): Detail[] {
  const work = useQuery({ queryKey: ["work", prRef.number], queryFn: () => api.work(prRef) });
  return [
    { label: "Clone directory", value: work.data?.path },
    { label: "PR branch", value: work.data?.branch ?? work.data?.expected_branch },
  ];
}
