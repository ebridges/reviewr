/** After a patch is committed over files you had edited, your edits are merged back on
 *  top. This lists the files where they conflicted and still have conflict markers. */
export function EditConflicts({ files }: { files: string[] | undefined }) {
  if (!files?.length) return null;
  return (
    <p className="small warn-text">
      Your uncommitted edits were put back after the commit, but these files conflict with it and have conflict markers
      (<span className="mono">&lt;&lt;&lt;&lt;&lt;&lt;&lt; your edits</span>) to resolve:{" "}
      {files.map((f, i) => <span key={f}>{i > 0 && ", "}<span className="mono">{f}</span></span>)}.
    </p>
  );
}
