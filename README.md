# `reviewr`

A local desktop app for working through review comments on very large GitHub pull
requests. Claude suggests a decision for each thread, drafts replies and writes fixes.
Nothing reaches GitHub until you submit.

## What it does

`reviewr` has three workflows for a PR, and the workflow switch moves between them:

1. **Review comments.** Decide what to do with each open thread, fix the ones that need
   a code change, and post every reply as one review. See
   [Working through review comments](#working-through-review-comments).
2. **Update from main.** Merge the base branch into the PR in a separate worktree. Rules
   settle the simple conflicts, the AI proposes code for the rest, and you resolve each
   one. Lint and tests can run on the merged files before the merge commit. See
   [Updating a PR from main](#updating-a-pr-from-main).
3. **Fix CI.** Fetch the failing checks and their logs, sort each failure into fix in
   this PR, re-run, or not this PR, draft fixes in a throwaway worktree, re-run jobs,
   and push. See [Fixing failing CI](#fixing-failing-ci).

## Working through review comments

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
   branch (or detached at the PR head), or one `reviewr` creates on the real branch if
   you have none. Your edits there are what `reviewr` sees. Agent items are drafted by
   the Claude Agent SDK in a throwaway worktree (file tools only, no shell), checked,
   and committed into your checkout touching only the patch's files. For your own
   items you pick which changed files go into the commit.
6. **Respond.** Review the summary comment and every reply. Submitting pushes the
   commits to the PR branch (fast-forward only), posts all replies as one review,
   resolves fixed and already-done threads, and re-requests review from reviewers who
   requested changes. A confirmation lists exactly what will be posted.

`reviewr` never stages or commits your unrelated changes, and checks run in a throwaway
worktree, not yours. Nothing reaches GitHub until you submit.

## Updating a PR from main

When the base branch has moved on, the header shows how far behind the PR is and how
many files would conflict (from `git merge-tree`, which touches no checkout). **Update
from main** follows the same pattern as review comments:

1. **Start.** `reviewr` merges main into a separate worktree at your checkout's current
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

## Fixing failing CI

When a PR's checks fail, the workflow switch shows **Fix CI** with the number failing.
It follows the same steps:

1. **Start.** The failing checks on the PR's latest commit, from one GraphQL query.
   **Analyze failures** downloads each failing job's log (REST; GraphQL has no logs,
   see D11 in `docs/github-api-strategy.md`) and keeps the step that failed.
2. **Triage.** The AI sorts the failures into *fix in this PR*, *re-run* (flaky or
   infrastructure) and *not this PR*, with the cause and the files to change.
3. **Fix.** One card per failure with GitHub's annotations and the failing step's log.
   Ask the AI for a fix (it works in a throwaway worktree; you see the diff, and Apply
   commits it to your checkout), re-run the job on GitHub, mark it fixed yourself, or
   dismiss it. Re-runs ask for confirmation.
4. **Finish.** Push the fix commits (with confirmation); CI runs again on GitHub.

## Install

Download the `.dmg` from the latest release on the repo's Releases page and drag
`reviewr` into Applications. It's built for Apple Silicon Macs. The app isn't signed
with an Apple Developer ID, so macOS blocks the first open. Clear the download
quarantine once:

```bash
xattr -dr com.apple.quarantine /Applications/reviewr.app
```

The same files are in GitHub Packages as `ghcr.io/ebridges/reviewr:<version>` (and
`latest`), which you can fetch with [oras](https://oras.land):

```bash
oras pull ghcr.io/ebridges/reviewr:latest
```

The app includes its Python backend and the Claude Code CLI. It needs `git`, the `gh`
CLI logged in, and a local clone of each repo you review. Its config is in
`~/.config/reviewr/`:

- `config.toml`, in the same format as `reviewr.toml.example`, with
  `repos."owner/name".path` set for each clone.
- `.env` with `ANTHROPIC_API_KEY=...`, unless you sign in with `ant auth login`.

To use the `reviewr` command (see [From the command line](#from-the-command-line)) with
the installed app:

```bash
ln -s /Applications/reviewr.app/Contents/Resources/bin/reviewr ~/.local/bin/reviewr
```

## Running from source

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

`reviewr` runs as a desktop app (Electron). From the project folder:

```bash
npm start
```

This builds the frontend, starts the backend on a free local port, and opens the
window. Quitting the app (⌘Q) stops the backend. Links to GitHub and Jira open in your
browser. The backend's output goes to
`~/Library/Application Support/reviewr/backend.log`.

If the frontend is already built and unchanged, `npm --prefix desktop run open` skips
the build and starts faster.

### From the command line

`bin/reviewr` opens the app from any folder. Put it on your PATH once, from the project
folder:

```bash
ln -s "$PWD/bin/reviewr" ~/.local/bin/reviewr
```

Then run `reviewr` in a checkout of a repo you review. If the branch checked out there
has an open PR, the app opens on that PR's Overview; otherwise it opens on the PR list.
If the app is already running, its window switches to the PR instead of a second copy
starting.

The PR is found from the folder's github.com remotes (`upstream` first, for forks) and
the branch's upstream name, and it must come from the repo the branch is pushed to. A
linked worktree works the same as the main clone. If the repo has no entry in
`reviewr.toml`, the command prints the lines to add.

```bash
reviewr                            # the PR for the current folder's branch
reviewr open ~/src/app             # the PR for another folder's branch
reviewr open owner/repo#123        # a specific PR (a PR URL works too)
reviewr open --no-launch           # print the PR without opening the app
```

The command rebuilds the frontend first if its sources changed since the last build.
`reviewr.toml` and `.env` in the project folder are found from any folder.

For working on `reviewr` itself, with hot reload:

```bash
npm run dev
```

This runs the Vite dev server (port 5173) and the backend with `--reload` (port 8765),
or reuses them if they're already running, and opens the app on them.

Without Electron, the same app runs in a browser: `cd backend && uv run reviewr serve`
after `npm --prefix frontend run build`, then open http://127.0.0.1:8765.

## Releasing

The version is kept the same in `package.json`, the `frontend/` and `desktop/` package
files, `backend/pyproject.toml` and `backend/reviewr/__init__.py`. To release, from an
up-to-date `main` with no uncommitted changes:

```bash
npm run release -- minor
```

This sets the next version (`patch`, `minor`, `major`, or an exact version such as
`1.2.0` or `1.2.0-rc.1`), commits `chore(release): v1.2.0`, tags `v1.2.0` and pushes
both. Add `--no-push` to stop before pushing. Giving the current version, as in
`npm run release -- 0.1.0` for the first release, tags it without a commit.

The tag starts the Release workflow (`.github/workflows/release.yml`) on an Apple
Silicon runner. It checks that the tag matches the version files, runs the tests,
builds the app, checks that the packaged app starts, and writes release notes from the
conventional commit messages since the previous tag (git-cliff, configured in
`cliff.toml`). Then it creates the GitHub Release with the `.dmg` and `.zip` and pushes
the same files to ghcr.io. A version with a suffix such as `-rc.1` is marked as a
prerelease and doesn't move `latest`.

`npm run notes` previews the next release's notes, and `npm run package` builds the
app locally into `desktop/release/`.

The CI workflow (`.github/workflows/ci.yml`) runs the backend tests and lint, the
frontend type check and build, and the version check on every push to `main` and every
PR.

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
- If you have uncommitted edits in `reviewr`'s worktree when you accept an agent patch,
  the patch may not apply cleanly.
- Editing settings (API key, editors, clones) happens in `.env` and `reviewr.toml`;
  the Settings page shows status and instructions.
- The packaged app is built for Apple Silicon Macs only, and isn't signed or notarized.
- The packaged app is large (the Claude Code CLI it includes is about 350 MB).
