/** Line diffs computed in the browser, for conflict previews that change as you pick
 *  an option or edit by hand. Conflict regions are small, so a plain LCS is enough. */

export type DLine = { k: " " | "+" | "-" | "ctx"; t: string };

const split = (s: string | null | undefined) => (!s ? [] : s.replace(/\n$/, "").split("\n"));

export function lineDiff(a: string | null | undefined, b: string | null | undefined): DLine[] {
  const A = split(a);
  const B = split(b);
  let p = 0;
  while (p < A.length && p < B.length && A[p] === B[p]) p++;
  let q = 0;
  while (q < A.length - p && q < B.length - p && A[A.length - 1 - q] === B[B.length - 1 - q]) q++;
  const a2 = A.slice(p, A.length - q);
  const b2 = B.slice(p, B.length - q);
  const n = a2.length;
  const m = b2.length;
  let mid: DLine[] = [];
  if (n * m > 4_000_000) {
    mid = [...a2.map((t) => ({ k: "-" as const, t })), ...b2.map((t) => ({ k: "+" as const, t }))];
  } else {
    // lcs[i][j]: longest common subsequence of a2[i:] and b2[j:]
    const w = m + 1;
    const lcs = new Uint32Array((n + 1) * w);
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        lcs[i * w + j] = a2[i] === b2[j] ? lcs[(i + 1) * w + j + 1] + 1 : Math.max(lcs[(i + 1) * w + j], lcs[i * w + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n || j < m) {
      // On ties, removals come before additions, as in git's output.
      if (i < n && j < m && a2[i] === b2[j]) { mid.push({ k: " ", t: a2[i] }); i++; j++; }
      else if (i < n && (j === m || lcs[(i + 1) * w + j] >= lcs[i * w + j + 1])) { mid.push({ k: "-", t: a2[i] }); i++; }
      else { mid.push({ k: "+", t: b2[j] }); j++; }
    }
  }
  return [
    ...A.slice(0, p).map((t) => ({ k: " " as const, t })),
    ...mid,
    ...A.slice(A.length - q).map((t) => ({ k: " " as const, t })),
  ];
}

export const plainLines = (s: string | null | undefined, k: DLine["k"] = " "): DLine[] => split(s).map((t) => ({ k, t }));

export function diffStats(lines: DLine[]) {
  return { added: lines.filter((l) => l.k === "+").length, removed: lines.filter((l) => l.k === "-").length };
}

/** For a removed line and the added line that replaces it: the part that differs. */
function changedSpan(a: string, b: string): [number, number, number, number] | null {
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  let q = 0;
  while (q < a.length - p && q < b.length - p && a[a.length - 1 - q] === b[b.length - 1 - q]) q++;
  // Only worth highlighting when most of the line is unchanged.
  if (p + q < Math.min(a.length, b.length) * 0.4) return null;
  return [p, a.length - q, p, b.length - q];
}

function Highlighted({ t, span }: { t: string; span?: [number, number] }) {
  if (!span || span[0] >= span[1]) return <>{t || " "}</>;
  return <>{t.slice(0, span[0])}<mark className="chg">{t.slice(span[0], span[1])}</mark>{t.slice(span[1])}</>;
}

/** Colored lines. Long runs of unchanged lines are folded; "ctx" lines (the code around
 *  a conflict, which isn't part of it) are dimmed. Where a block of removed lines is
 *  followed by as many added lines, the changed characters in each pair are marked. */
export function LineDiffView({ lines, empty = "(no lines)", fold = 8 }: { lines: DLine[]; empty?: string; fold?: number }) {
  if (!lines.length) return <pre className="code wrap"><div className="meta">{empty}</div></pre>;
  const spans = new Map<number, [number, number]>();
  for (let i = 0; i < lines.length; ) {
    if (lines[i].k !== "-") { i++; continue; }
    let d = i;
    while (d < lines.length && lines[d].k === "-") d++;
    let a = d;
    while (a < lines.length && lines[a].k === "+") a++;
    if (d - i === a - d) {
      for (let x = 0; x < d - i; x++) {
        const sp = changedSpan(lines[i + x].t, lines[d + x].t);
        if (sp) { spans.set(i + x, [sp[0], sp[1]]); spans.set(d + x, [sp[2], sp[3]]); }
      }
    }
    i = a;
  }
  const out: ({ l: DLine; i: number } | { fold: number })[] = [];
  let run: { l: DLine; i: number }[] = [];
  const flush = () => {
    if (run.length > fold * 2 + 1) out.push(...run.slice(0, fold), { fold: run.length - fold * 2 }, ...run.slice(-fold));
    else out.push(...run);
    run = [];
  };
  lines.forEach((l, i) => {
    if (l.k === " ") run.push({ l, i });
    else { flush(); out.push({ l, i }); }
  });
  flush();
  return (
    <pre className="code wrap">
      {out.map((x, n) =>
        "fold" in x ? (
          <div key={n} className="meta">⋯ {x.fold} unchanged lines</div>
        ) : (
          <div key={n} className={x.l.k === "+" ? "add" : x.l.k === "-" ? "del" : x.l.k === "ctx" ? "ctx" : ""}>
            <span className="sign" aria-hidden="true">{x.l.k === "+" ? "+" : x.l.k === "-" ? "−" : " "}</span>
            <Highlighted t={x.l.t} span={spans.get(x.i)} />
          </div>
        ),
      )}
    </pre>
  );
}
