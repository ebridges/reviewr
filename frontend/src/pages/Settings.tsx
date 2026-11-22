import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api, type ConfigData, type RepoConfigData, type SecretName, type Settings as SettingsData } from "../api";
import { ProviderIcon } from "../components/AIIcons";
import { StoragePanel } from "../components/StoragePanel";
import { storageSet } from "../lib";

const MODELS = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2"];
const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

function Status({ ok, text }: { ok: boolean | null; text: string }) {
  return <span className={`pill ${ok === null ? "" : ok ? "ok" : "warn"}`}>{text}</span>;
}

/** argv list <-> one line, with double quotes around arguments that contain spaces. */
const argvText = (argv: string[]) => argv.map((a) => (/\s/.test(a) ? `"${a}"` : a)).join(" ");
const parseArgv = (text: string) => (text.match(/"[^"]*"|\S+/g) ?? []).map((a) => a.replace(/^"(.*)"$/, "$1"));
const lines = (list: string[]) => list.join("\n");
const parseLines = (text: string) => text.split("\n").map((l) => l.trim()).filter(Boolean);

export function Settings() {
  const qc = useQueryClient();
  const s = useQuery({
    queryKey: ["settings"],
    queryFn: api.settings,
    // While `ant auth login` waits for the browser, check every two seconds.
    refetchInterval: (q) => (q.state.data?.claude.login_running ? 2000 : false),
  });
  const ai = useQuery({ queryKey: ["ai"], queryFn: api.ai });
  const d = s.data;

  // Edits are kept in a draft and saved together.
  const [draft, setDraft] = useState<ConfigData | null>(null);
  useEffect(() => { if (d && !draft) setDraft(structuredClone(d.config)); }, [d, draft]);
  const dirty = useMemo(() => !!d && !!draft && JSON.stringify(d.config) !== JSON.stringify(draft), [d, draft]);
  const edit = (change: (c: ConfigData) => void) => setDraft((c) => { if (!c) return c; const n = structuredClone(c); change(n); return n; });

  const onSaved = (r: SettingsData) => { qc.setQueryData(["settings"], r); setDraft(structuredClone(r.config)); qc.invalidateQueries({ queryKey: ["ai"] }); qc.invalidateQueries({ queryKey: ["config"] }); };
  const save = useMutation({ mutationFn: () => api.saveConfig(draft!), onSuccess: (r) => { onSaved(r); storageSet("reviewr:editor", r.config.default_editor); } });
  const choose = useMutation({ mutationFn: api.setAI, onSuccess: (r) => { qc.setQueryData(["ai"], r); qc.invalidateQueries({ queryKey: ["settings"] }); } });
  const login = useMutation({ mutationFn: api.claudeLogin, onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }) });

  if (!d || !draft) {
    return <main className="page narrow">{s.error ? <div className="banner error">{(s.error as Error).message}</div> : <p className="muted">Loading settings…</p>}</main>;
  }

  const UseButton = ({ p, ready }: { p: "claude" | "codex"; ready: boolean }) =>
    ai.data?.provider === p ? (
      <span className="pill ok">In use</span>
    ) : (
      <button className="btn sm" disabled={!ready || choose.isPending} onClick={() => choose.mutate(p)}>Use {p === "claude" ? "Claude" : "Codex"}</button>
    );

  return (
    <main className="page narrow settings">
      <div className="page-head">
        <h1 className="grow">Settings</h1>
        <button className="btn" onClick={() => s.refetch()} disabled={s.isFetching}>{s.isFetching ? "Checking…" : "Check again"}</button>
      </div>
      {s.error && <div className="banner error">{(s.error as Error).message}</div>}
      {choose.error && <div className="banner error">{(choose.error as Error).message}</div>}

      <div className="integration-group">
        <h2>AI</h2>
        <p className="muted small">
          One provider does every AI step: decision suggestions, reply drafts, work-item planning, patch checks, and the coding agent that
          drafts fixes. Switch here or with the Claude / Codex switch in the header.
        </p>
        <section className="card stack-gap">
          <div className="row-between">
            <h3 className="h3 h3-icon"><ProviderIcon provider="claude" size={20} />Claude (Anthropic API)</h3>
            <span className="row-wrap">
              <Status ok={d.claude.ready} text={d.claude.detail} />
              <UseButton p="claude" ready={!!ai.data?.claude_ready} />
            </span>
          </div>
          <div className="row-wrap">
            <label className="field grow">
              Model
              <input className="input mono" list="claude-models" value={draft.llm.model} onChange={(e) => edit((c) => { c.llm.model = e.target.value; })} />
              <datalist id="claude-models">{MODELS.map((m) => <option key={m} value={m} />)}</datalist>
            </label>
            <label className="field">
              Effort
              <select className="input" value={draft.llm.effort} onChange={(e) => edit((c) => { c.llm.effort = e.target.value; })}>
                {EFFORTS.map((x) => <option key={x}>{x}</option>)}
              </select>
            </label>
          </div>
          <p className="muted small">
            The coding agent edits files in a throwaway worktree with Anthropic's text editor tool, plus file search. It can't run commands.
          </p>
          <div className="field">
            Sign in with
            <div className="row-wrap" role="radiogroup">
              {(["ant", "api_key"] as const).map((m) => (
                <label key={m} className="radio">
                  <input type="radio" checked={draft.ai.claude_auth === m} onChange={() => edit((c) => { c.ai.claude_auth = m; })} />
                  {m === "ant" ? "The ant CLI (recommended)" : "An API key"}
                </label>
              ))}
            </div>
          </div>
          {draft.ai.claude_auth === "ant" ? (
            <div className="option stack-gap">
              <p className="muted small">
                Sign in once in your browser; there's no key to copy or store. reviewr uses the profile the ant CLI saves.
                {d.config.ai.claude_auth !== "ant" && " Save to switch to this sign-in."}
              </p>
              {d.claude.ant_installed ? (
                <div className="row-wrap">
                  <button className="btn sm" onClick={() => login.mutate()} disabled={login.isPending || d.claude.login_running}>
                    {d.claude.login_running ? "Waiting for the browser…" : "Sign in with the ant CLI"}
                  </button>
                  {login.error && <span className="error small">{(login.error as Error).message}</span>}
                </div>
              ) : (
                <>
                  <p className="small">Install the ant CLI first, then come back and sign in:</p>
                  <pre className="cmd">brew install anthropics/tap/ant</pre>
                </>
              )}
            </div>
          ) : (
            <SecretField name="anthropic_api_key" label="API key" saved={d.secrets.anthropic_api_key} placeholder="sk-ant-…"
              note="Saved in your macOS Keychain, never in a file." onSaved={onSaved} />
          )}
        </section>

        <section className="card stack-gap">
          <div className="row-between">
            <h3 className="h3 h3-icon"><ProviderIcon provider="codex" size={20} />Codex (OpenAI)</h3>
            <span className="row-wrap">
              <Status ok={!!d.codex.status} text={!d.codex.installed ? "Not installed" : d.codex.status ? `Signed in · ${d.codex.status}` : "Installed, not signed in"} />
              <UseButton p="codex" ready={!!ai.data?.codex_ready} />
            </span>
          </div>
          <p className="muted small">
            Runs through the Codex CLI with your <span className="mono">codex login</span>. Structured steps run read-only in an empty folder; the
            coding agent runs in Codex's workspace-write sandbox, which can edit files and run commands in the draft worktree, without network access.
          </p>
          <label className="field">
            Model (empty: the Codex CLI's default)
            <input className="input mono" value={draft.ai.codex_model ?? ""} onChange={(e) => edit((c) => { c.ai.codex_model = e.target.value.trim() || null; })} />
          </label>
          {!d.codex.status && <pre className="cmd">codex login</pre>}
        </section>
      </div>

      <div className="integration-group">
        <h2>Workspace</h2>
        <section className="card stack-gap">
          <label className="field">
            Workspace folder
            <input className="input mono" value={draft.workspace_dir} onChange={(e) => edit((c) => { c.workspace_dir = e.target.value; })} />
          </label>
          <p className="muted small">
            When you open a pull request for a repository reviewr hasn't set up, it looks for a clone in this folder, or clones it to
            <span className="mono"> {draft.workspace_dir}/owner/repo</span>, then checks out the branch, finds the Node and Python versions and
            installs dependencies.
          </p>
        </section>
        {Object.keys(draft.repos).length === 0 && <p className="muted small">No repositories set up yet. Open a pull request to set one up.</p>}
        {Object.entries(draft.repos).sort(([a], [b]) => a.localeCompare(b)).map(([slug, repo]) => (
          <RepoCard key={slug} slug={slug} repo={repo} steps={d.guidance}
            onChange={(r) => edit((c) => { c.repos[slug] = r; })}
            onRemove={() => edit((c) => { delete c.repos[slug]; })} />
        ))}
      </div>

      <div className="integration-group">
        <h2>Guidance</h2>
        <p className="muted small">
          Instructions added to each AI step, after reviewr's own. Use them for your team's conventions: how replies should sound, how to group
          work, what the coding agent should always or never do. Each repository can add its own (above), and a repository can commit
          guidance in <span className="mono">.reviewr/guidance/&lt;step&gt;.md</span>. Coding steps also read the repo's
          <span className="mono"> CLAUDE.md</span> or <span className="mono">AGENTS.md</span>, and the agent can open its
          <span className="mono"> .claude/skills</span>.
        </p>
        <section className="card stack-gap">
          {Object.entries(d.guidance).map(([step, g]) => (
            <div key={step} className="field">
              {g.label}
              {g.builtin && (
                <details className="builtin">
                  <summary className="muted small">reviewr's built-in guidance</summary>
                  <pre className="builtin-text">{g.builtin}</pre>
                </details>
              )}
              <textarea className="input" rows={2} placeholder="Your guidance for every repository"
                value={draft.guidance[step] ?? ""} onChange={(e) => edit((c) => { c.guidance[step] = e.target.value; })} />
            </div>
          ))}
        </section>
      </div>

      <div className="integration-group">
        <h2>Version control</h2>
        <section className="card stack-gap">
          <div className="row-between">
            <h3 className="h3">GitHub</h3>
            <Status ok={!!d.github} text={d.github ? `Signed in via ${d.github}${d.github_login ? ` as ${d.github_login}` : ""}` : "Not signed in"} />
          </div>
          <p className="muted small">Lists your PRs, reads review threads, clones repositories, pushes commits and posts replies, with the GitHub CLI's login.</p>
          {!d.github && <pre className="cmd">gh auth login --scopes repo,read:org</pre>}
          {d.rate_limit && <p className="muted small">GraphQL budget: {d.rate_limit.remaining.toLocaleString()} points left this hour.</p>}
        </section>
      </div>

      <div className="integration-group">
        <h2>Issue tracking</h2>
        <section className="card stack-gap">
          <div className="row-between">
            <h3 className="h3">Jira</h3>
            <Status ok={d.jira.configured ? !!d.jira.user : false}
              text={!d.jira.configured ? "Not set up" : d.jira.user ? `Signed in as ${d.jira.user}` : "Set up, can't connect"} />
          </div>
          <p className="muted small">Plan's “Create a JIRA” files one issue for the threads you deferred, and the deferred replies name it.</p>
          {d.jira.error && <p className="error small">{d.jira.error}</p>}
          {draft.jira ? (
            <>
              <div className="grid-2">
                <label className="field">Site<input className="input" placeholder="https://yourcompany.atlassian.net" value={draft.jira.base_url} onChange={(e) => edit((c) => { c.jira!.base_url = e.target.value; })} /></label>
                <label className="field">Project key<input className="input" value={draft.jira.project} onChange={(e) => edit((c) => { c.jira!.project = e.target.value; })} /></label>
                <label className="field">Issue type<input className="input" value={draft.jira.issue_type} onChange={(e) => edit((c) => { c.jira!.issue_type = e.target.value; })} /></label>
                <label className="field">Atlassian email<input className="input" value={draft.jira.email ?? ""} onChange={(e) => edit((c) => { c.jira!.email = e.target.value || null; })} /></label>
              </div>
              <SecretField name="jira_api_token" label="API token" saved={d.secrets.jira_api_token}
                note="Create one at id.atlassian.com → Security → API tokens. Saved in your macOS Keychain." onSaved={onSaved} />
              <div><button className="link small" onClick={() => edit((c) => { c.jira = null; })}>Remove Jira</button></div>
            </>
          ) : (
            <div><button className="btn sm" onClick={() => edit((c) => { c.jira = { base_url: "", project: "", issue_type: "Task", email: null }; })}>Set up Jira</button></div>
          )}
        </section>
      </div>

      <div className="integration-group">
        <h2>This machine</h2>
        <section className="card stack-gap">
          <h3 className="h3">Editor</h3>
          <label className="field">
            Open files with
            <select className="input" value={draft.default_editor} onChange={(e) => edit((c) => { c.default_editor = e.target.value; })}>
              {Object.keys(draft.editors).map((e) => <option key={e}>{e}</option>)}
            </select>
          </label>
          <EditorList editors={draft.editors} onChange={(editors) => edit((c) => { c.editors = editors; })} />
        </section>
        <StoragePanel />
        <p className="muted small">Settings are saved in <span className="mono">{d.settings_path}</span>; secrets are in your Keychain.</p>
      </div>

      {(dirty || save.error) && (
        <div className="save-bar">
          {save.error ? <span className="error small grow">{(save.error as Error).message}</span> : <span className="small grow">You have unsaved changes.</span>}
          <button className="btn" onClick={() => setDraft(structuredClone(d.config))} disabled={save.isPending}>Discard</button>
          <button className="btn primary" onClick={() => save.mutate()} disabled={save.isPending}>{save.isPending ? "Saving…" : "Save changes"}</button>
        </div>
      )}
    </main>
  );
}

function SecretField({ name, label, saved, note, placeholder, onSaved }: {
  name: SecretName; label: string; saved: boolean; note: string; placeholder?: string; onSaved: (r: SettingsData) => void;
}) {
  const [value, setValue] = useState("");
  const set = useMutation({ mutationFn: () => api.setSecret(name, value), onSuccess: (r) => { setValue(""); onSaved(r); } });
  const remove = useMutation({ mutationFn: () => api.deleteSecret(name), onSuccess: onSaved });
  return (
    <div className="field">
      {label} {saved && <span className="pill ok">Saved</span>}
      <div className="row-wrap">
        <input className="input mono grow" type="password" autoComplete="off" placeholder={saved ? "Enter a new value to replace it" : placeholder}
          value={value} onChange={(e) => setValue(e.target.value)} />
        <button className="btn sm" disabled={!value.trim() || set.isPending} onClick={() => set.mutate()}>Save</button>
        {saved && <button className="btn sm" disabled={remove.isPending} onClick={() => remove.mutate()}>Remove</button>}
      </div>
      <span className="muted small">{note}</span>
      {(set.error || remove.error) && <span className="error small">{((set.error || remove.error) as Error).message}</span>}
    </div>
  );
}

function EditorList({ editors, onChange }: { editors: Record<string, string[]>; onChange: (e: Record<string, string[]>) => void }) {
  const [name, setName] = useState("");
  return (
    <div className="stack-gap">
      <p className="muted small">
        Each editor is a command. Placeholders: <span className="mono">{"{root}"}</span> (the worktree, opened as the project),
        <span className="mono"> {"{path}"}</span> (the file) and <span className="mono">{"{line}"}</span>.
      </p>
      {Object.entries(editors).map(([key, argv]) => (
        <div key={key} className="row-wrap">
          <span className="mono editor-name">{key}</span>
          <input className="input mono grow" value={argvText(argv)} onChange={(e) => onChange({ ...editors, [key]: parseArgv(e.target.value) })} />
          <button className="link small" onClick={() => { const n = { ...editors }; delete n[key]; onChange(n); }}>Remove</button>
        </div>
      ))}
      <div className="row-wrap">
        <input className="input" placeholder="Add an editor, e.g. zed" value={name} onChange={(e) => setName(e.target.value)} />
        <button className="btn sm" disabled={!name.trim() || name in editors}
          onClick={() => { onChange({ ...editors, [name.trim()]: [name.trim(), "{root}", "{path}:{line}"] }); setName(""); }}>Add</button>
      </div>
    </div>
  );
}

function RepoCard({ slug, repo, steps, onChange, onRemove }: {
  slug: string; repo: RepoConfigData; steps: SettingsData["guidance"];
  onChange: (r: RepoConfigData) => void; onRemove: () => void;
}) {
  const set = (patch: Partial<RepoConfigData>) => onChange({ ...repo, ...patch });
  return (
    <section className="card stack-gap">
      <div className="row-between">
        <h3 className="h3 mono">{slug}</h3>
        <button className="link small" onClick={onRemove}>Remove</button>
      </div>
      <label className="field">Clone<input className="input mono" value={repo.path} onChange={(e) => set({ path: e.target.value })} /></label>
      {Object.keys(repo.toolchain).length > 0 && (
        <p className="muted small">Toolchain from setup: {Object.entries(repo.toolchain).map(([k, v]) => `${k} ${v}`).join(", ")}.</p>
      )}
      <p className="muted small">
        Dependencies installed per branch: {repo.envs.length ? repo.envs.map((e) => e.name).join(", ") : "none"}. Checks per work item:{" "}
        {repo.checks.length ? repo.checks.map((c) => c.name).join(", ") : "none"}. Workspace setup fills these in when they're empty.
      </p>
      <details>
        <summary className="small">Installs, checks and PATH</summary>
        <div className="stack-gap details-body">
          <label className="field">
            Put first on PATH (one folder per line)
            <textarea className="input mono" rows={2} value={lines(repo.path_prepend)} onChange={(e) => set({ path_prepend: parseLines(e.target.value) })} />
          </label>
          <label className="field">
            Never run per work item (test file globs, one per line)
            <textarea className="input mono" rows={2} value={lines(repo.exclude_tests)} onChange={(e) => set({ exclude_tests: parseLines(e.target.value) })} />
          </label>
          <JsonField label="Dependency installs" value={repo.envs} onChange={(envs) => set({ envs })} />
          <JsonField label="Checks" value={repo.checks} onChange={(checks) => set({ checks })} />
        </div>
      </details>
      <details>
        <summary className="small">Guidance for this repository</summary>
        <div className="stack-gap details-body">
          {Object.entries(steps).map(([step, g]) => (
            <label key={step} className="field">
              {g.label}
              <textarea className="input" rows={2} value={repo.guidance[step] ?? ""}
                onChange={(e) => set({ guidance: { ...repo.guidance, [step]: e.target.value } })} />
            </label>
          ))}
        </div>
      </details>
    </section>
  );
}

/** Edit a list as JSON; the value only changes when the text parses. */
function JsonField<T>({ label, value, onChange }: { label: string; value: T; onChange: (v: T) => void }) {
  const [text, setText] = useState(() => JSON.stringify(value, null, 2));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    try { if (JSON.stringify(JSON.parse(text)) !== JSON.stringify(value)) setText(JSON.stringify(value, null, 2)); } catch { /* keep the text being edited */ }
  }, [value]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <label className="field">
      {label} (JSON)
      <textarea className="input mono" rows={8} value={text} spellCheck={false}
        onChange={(e) => {
          setText(e.target.value);
          try { onChange(JSON.parse(e.target.value)); setError(null); } catch (err) { setError((err as Error).message); }
        }} />
      {error && <span className="error small">{error}</span>}
    </label>
  );
}
