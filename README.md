# reviewr

A local app for working through review comments on very large pull requests.

The work is organized as steps. Each open thread moves from open, to decided, to done,
to replied:

1. **Pull requests.** Your open PRs, or PRs where your review is requested.
2. **Overview.** One Claude call suggests a decision for every open thread. Stale
   threads (GitHub's "outdated") are listed separately: the code under them changed,
   which doesn't mean they were addressed.
3. **Triage.** Confirm or change each decision: Fix, Reply only, Push back, Already
   done, or Defer. Card view for reading, with keyboard shortcuts and a git trace
   showing where a stale comment originally pointed. List view for marking many
   threads at once. Choosing Reply only, Push back or Defer drafts a reply.
4. **Plan.** Claude groups the Fix threads into work items, one commit each. Choose
   per item whether the agent drafts it or you do it.
5. **Fix.** Work happens in your checkout of the PR branch: a worktree of yours on the
   branch (or detached at the PR head), or one reviewr creates on the real branch if
   you have none. Your edits there are what reviewr sees. Agent items are drafted by
   the Claude Agent SDK in a throwaway worktree (file tools only, no shell), checked,
   and committed into your checkout touching only the patch's files. For your own
   items you pick which changed files go into the commit.
6. **Respond.** Review the summary comment and every reply. Submitting pushes the
   commits to the PR branch (fast-forward only), posts all replies as one review,
   resolves fixed and already-done threads, and re-requests review from reviewers who
   requested changes. A confirmation lists exactly what will be posted.

reviewr never stages or commits your unrelated changes, and checks run in a throwaway
worktree, not yours. Nothing reaches GitHub until you submit.

## Updating a PR from main

When the base branch has moved on, the header shows how far behind the PR is and how
many files would conflict (from `git merge-tree`, which touches no checkout). **Update
from main** follows the same pattern as review comments:

1. **Start.** reviewr merges main into a separate worktree at your checkout's current
   commit, so unpushed Fix commits are included and your files aren't touched.
2. **Triage.** Each conflicted region is one conflict. Rules settle the simple ones
   (identical changes, whitespace-only changes); the AI sorts the rest into *merge
   automatically*, *needs your input* and *proposed code change*, and writes combined
   code for the last group. Recategorize any of them, and accept all the automatic
   ones at once after looking at their results.
3. **Resolve.** One card per conflict: what this PR changed and what main changed (each
   against the code they started from, with the commits that touched the file), the
   options (keep this PR's version, take main's, keep both in either order, the AI's
   version, or your own edit), and the code after the merge for the selected option,
   compared with either side. You can tell the AI how to combine the two sides and get a
   new version. Lockfiles, deleted files and binary files are resolved per file.
4. **Finish.** Each conflicted file before and after the merge, optional lint and tests
   on those files with the merged code's dependencies, and **Commit merge**. That makes
   the merge commit and fast-forwards your checkout to it; if your uncommitted edits
   touch the same files, or your checkout moved since the update started, nothing is
   changed and you're told why. Pushing is a separate, confirmed step (or Respond pushes
   it with your replies).

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), Node 20+, the `gh` CLI
logged in, Claude Code installed (the Agent SDK uses it), and a local clone of each
repo you review.

```bash
cp reviewr.toml.example reviewr.toml   # then set repos."owner/name".path
```

Install everything (backend, frontend, desktop app) once:

```bash
npm run setup
```

Claude credentials: put `ANTHROPIC_API_KEY=...` in `.env` at the project root
(gitignored), or sign in with `ant auth login`. The Settings page shows which source
is in use.

## Running

reviewr runs as a desktop app (Electron). From the project folder:

```bash
npm start
```

This builds the frontend, starts the backend on a free local port, and opens the
window. Quitting the app (⌘Q) stops the backend. Links to GitHub and Jira open in your
browser. The backend's output goes to
`~/Library/Application Support/reviewr/backend.log`.

If the frontend is already built and unchanged, `npm --prefix desktop run open` skips
the build and starts faster.

For working on reviewr itself, with hot reload:

```bash
npm run dev
```

This runs the Vite dev server (port 5173) and the backend with `--reload` (port 8765),
or reuses them if they're already running, and opens the app on them.

Without Electron, the same app runs in a browser: `cd backend && uv run reviewr serve`
after `npm --prefix frontend run build`, then open http://127.0.0.1:8765.

## Layout

```
docs/github-api-strategy.md   why reviewr uses GraphQL only, and the cost of each call (D1-D10)
backend/reviewr/
  github/                     GraphQL client, .graphql queries and mutations, PR fetch and list
  git.py                      diffs, stale-comment tracing, worktrees
  state.py                    per-PR review state: suggestions, decisions, plan
  store.py                    SQLite cache and state (~/.local/share/reviewr/reviewr.sqlite3)
  llm/                        Claude calls: triage suggestions, replies, planning, patch checks
  agent.py, fixwork.py        drafting work items with the Agent SDK, committing
  merge.py, llm/conflicts.py  updating from main: merge worktree, conflicts, AI triage
  submit.py                   the Respond step: dry run and posting
  api.py                      FastAPI routes used by the frontend
frontend/src/pages/           one file per step, plus Settings
desktop/main.cjs              the Electron app: starts the backend, opens the window
```

## Known limits

- The agent itself has file tools only. Lint and tests run after it finishes, scoped to
  the files the work item changes (configured per repo in `reviewr.toml`); end-to-end
  suites are never run per item.
- Checks use dependencies installed from the PR branch's own lockfiles (the `envs`
  section in `reviewr.toml`), stored under `~/.local/share/reviewr/envs/`. Every worktree
  links to one shared install per set of lockfiles; a changed lockfile starts from a
  copy-on-write clone of the previous install and only applies the differences.
  Settings → Storage previews and runs a cleanup of installs no open PR needs and of
  worktrees for merged or closed PRs. Your clone's installed packages are never used
  or changed.
- If you have uncommitted edits in reviewr's worktree when you accept an agent patch,
  the patch may not apply cleanly.
- Editing settings (API key, editors, clones) happens in `.env` and `reviewr.toml`;
  the Settings page shows status and instructions.
- The desktop app runs from this folder (it isn't packaged as a standalone .app), and
  needs uv, Node and gh installed.
