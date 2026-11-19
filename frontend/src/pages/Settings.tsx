import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { ProviderIcon } from "../components/AIIcons";
import { StoragePanel } from "../components/StoragePanel";
import { storageGet, storageSet } from "../lib";

function Status({ ok, text }: { ok: boolean | null; text: string }) {
  return <span className={`pill ${ok === null ? "" : ok ? "ok" : "warn"}`}>{text}</span>;
}

export function Settings() {
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const [editor, setEditor] = useState(storageGet("reviewr:editor") ?? "");
  const d = s.data;
  const qc = useQueryClient();
  const ai = useQuery({ queryKey: ["ai"], queryFn: api.ai });
  const choose = useMutation({ mutationFn: api.setAI, onSuccess: (r) => qc.setQueryData(["ai"], r) });
  const UseButton = ({ p, ready }: { p: "claude" | "codex"; ready: boolean }) =>
    ai.data?.provider === p ? (
      <span className="pill ok">In use</span>
    ) : (
      <button className="btn sm" disabled={!ready || choose.isPending} onClick={() => choose.mutate(p)}>Use {p === "claude" ? "Claude" : "Codex"}</button>
    );

  return (
    <main className="page narrow">
      <div className="page-head">
        <h1 className="grow">Settings</h1>
        <button className="btn" onClick={() => s.refetch()} disabled={s.isFetching}>{s.isFetching ? "Checking…" : "Check again"}</button>
      </div>
      {s.error && <div className="banner error">{(s.error as Error).message}</div>}

      <div className="integration-group">
        <h2>AI integrations</h2>
        <p className="muted small">
          One of these does every AI step: decision suggestions, reply drafts, work-item planning, patch checks, and the coding agent
          that drafts fixes. Switch here or with the Claude / Codex switch in the header.
        </p>
        {choose.error && <div className="banner error">{(choose.error as Error).message}</div>}
        <section className="card stack-gap">
          <div className="row-between">
            <h3 className="h3 h3-icon"><ProviderIcon provider="claude" size={20} />Claude (Anthropic)</h3>
            <span className="row-wrap">
              <Status ok={d ? !!d.anthropic : null} text={d ? (d.anthropic ? `Connected · ${d.anthropic}` : "Not connected") : "…"} />
              <UseButton p="claude" ready={!!ai.data?.claude_ready} />
            </span>
          </div>
          <p className="muted small">
            Model: <span className="mono">{d?.model}</span>. The coding agent is the Claude Agent SDK with file tools only, so it can read
            and edit files in the draft worktree but can't run commands.
          </p>
          <div className="option">
            <strong className="small">Sign in with the ant CLI (recommended)</strong>
            <p className="muted small">No key to copy or store. The SDK reads the saved login automatically.</p>
            <pre className="cmd">ant auth login</pre>
          </div>
          <div className="option">
            <strong className="small">Use an API key</strong>
            <p className="muted small">Add it to <span className="mono">.env</span> in the project folder (gitignored), then restart the backend.</p>
            <pre className="cmd">ANTHROPIC_API_KEY=sk-ant-...</pre>
          </div>
        </section>
        <section className="card stack-gap">
          <div className="row-between">
            <h3 className="h3 h3-icon"><ProviderIcon provider="codex" size={20} />Codex (OpenAI)</h3>
            <span className="row-wrap">
              <Status
                ok={d ? !!d.codex.status : null}
                text={d ? (!d.codex.installed ? "Not installed" : d.codex.status ? `Connected · ${d.codex.status}` : "Installed, not signed in") : "…"}
              />
              <UseButton p="codex" ready={!!ai.data?.codex_ready} />
            </span>
          </div>
          <p className="muted small">
            Runs through the Codex CLI with your <span className="mono">codex login</span>, using its default model (set{" "}
            <span className="mono">codex_model</span> under <span className="mono">[ai]</span> in reviewr.toml to change it). Structured
            steps run read-only in an empty folder. The coding agent runs in Codex's workspace-write sandbox: it can edit files and run
            commands inside the draft worktree, with no network access.
          </p>
          <pre className="cmd">codex login</pre>
        </section>
      </div>

      <div className="integration-group">
        <h2>Version control</h2>
        <section className="card stack-gap">
          <div className="row-between">
            <h3 className="h3">GitHub</h3>
            <Status ok={d ? !!d.github : null} text={d ? (d.github ? `Connected via ${d.github}${d.github_login ? ` as ${d.github_login}` : ""}` : "Not connected") : "…"} />
          </div>
          <p className="muted small">Lists your PRs, reads review threads, pushes commits and posts replies.</p>
          <div className="option">
            <strong className="small">Use the GitHub CLI login (recommended)</strong>
            <p className="muted small">reviewr asks gh for a token when it starts. If your organization uses SSO, authorize the token when gh prompts you.</p>
            <pre className="cmd">gh auth login --scopes repo,read:org</pre>
          </div>
          <div className="option">
            <strong className="small">Use a fine-grained personal access token</strong>
            <p className="muted small">Permissions: Pull requests read and write, Contents read and write (for pushing), Metadata read. Set it as <span className="mono">GITHUB_TOKEN</span>; it overrides gh.</p>
          </div>
          {d?.rate_limit && <p className="muted small">GraphQL budget: {d.rate_limit.remaining.toLocaleString()} points left this hour.</p>}
        </section>
      </div>

      <div className="integration-group">
        <h2>Issue tracking</h2>
        <section className="card stack-gap">
          <div className="row-between">
            <h3 className="h3">Jira</h3>
            <Status
              ok={d ? (d.jira.configured ? !!d.jira.user : false) : null}
              text={d ? (!d.jira.configured ? "Not configured" : d.jira.user ? `Connected as ${d.jira.user}` : "Configured, can't connect") : "…"}
            />
          </div>
          <p className="muted small">
            Plan's “Create a JIRA” button files one issue for the threads you deferred, linking each one, and the deferred replies then name the issue.
          </p>
          {d?.jira.configured && (
            <p className="small">
              Project <span className="mono">{d.jira.project}</span> · issue type {d.jira.issue_type} · <a href={d.jira.base_url} target="_blank" rel="noreferrer">{d.jira.base_url}</a>
            </p>
          )}
          {d?.jira.error && <p className="error small">{d.jira.error}</p>}
          <div className="option">
            <strong className="small">Set up</strong>
            <p className="muted small">
              Add a <span className="mono">[jira]</span> section to <span className="mono">reviewr.toml</span>, and your Atlassian email and
              an API token (id.atlassian.com → Security → API tokens) to <span className="mono">.env</span>. Restart the backend.
            </p>
            <pre className="cmd">{`[jira]\nbase_url = "https://yourcompany.atlassian.net"\nproject = "MOS"\nissue_type = "Task"`}</pre>
            <pre className="cmd">{`JIRA_EMAIL=you@example.com\nJIRA_API_TOKEN=...`}</pre>
          </div>
        </section>
      </div>

      <div className="integration-group">
        <h2>This machine</h2>
        <section className="card stack-gap">
          <h3 className="h3">Editor</h3>
          <label className="field">
            Open files with
            <select className="input" value={editor || d?.default_editor || ""} onChange={(e) => { setEditor(e.target.value); storageSet("reviewr:editor", e.target.value); }}>
              {(d?.editors ?? []).map((e) => <option key={e}>{e}</option>)}
            </select>
          </label>
          <p className="muted small">Add other editors under <span className="mono">[editors]</span> in <span className="mono">reviewr.toml</span>.</p>
        </section>
        <section className="card stack-gap">
          <h3 className="h3">Local clones and checks</h3>
          {d && Object.keys(d.repos).length === 0 && <p className="muted small">None configured.</p>}
          {d && Object.entries(d.repos).map(([slug, r]) => (
            <div key={slug} className="option">
              <div className="row-between small"><strong>{slug}</strong><span className="mono muted">{r.path}</span></div>
              <p className="muted small">
                Dependencies installed per branch: {r.envs.length ? r.envs.join(", ") : "none configured"}. Checks per work item: {r.checks.length ? r.checks.join(", ") : "none configured"}. Never run per item: {r.exclude_tests.join(", ")}.
              </p>
            </div>
          ))}
          <p className="muted small">Configure clones and checks under <span className="mono">[repos."owner/name"]</span> in <span className="mono">reviewr.toml</span>.</p>
        </section>
        <StoragePanel />
      </div>
    </main>
  );
}
