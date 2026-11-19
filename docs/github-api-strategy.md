# GitHub API strategy: REST vs. GraphQL

This document records how reviewr talks to GitHub and why. It was worked out in
two passes. The first pass optimizes only for rate limits and request cost. The
second pass revisits each decision for developer experience (how easy the code
is to use and reason about). The table at the end gives the final decision and
says which basis it was chosen on.

Decision IDs (D1, D2, ...) are referenced from code comments, so a reader of the
code can find the reasoning here.

## Constraints

From GitHub's documentation ([REST limits][rest], [GraphQL limits][gql]):

| Limit | REST | GraphQL |
|---|---|---|
| Primary budget for a user token | 5,000 requests/hour | 5,000 points/hour |
| Cost of a read | 1 request per page (max 100 items) | `ceil(connection requests / 100)`, minimum 1 point |
| Conditional request (`If-None-Match`) that returns 304 | Free | Not supported |
| Secondary: points per minute | 900 | 2,000 |
| Secondary: cost of a write | 5 points | 5 points per mutation |
| Secondary: content creation | 80/minute, 500/hour (shared) | same |

The two primary budgets are separate. Using both APIs gives 10,000 units per
hour, but that only matters if one of them is close to running out.

Measured on mosaic-pe/mosaic-app#3307 (366 files, 88 review threads, 108 review
comments, 24 reviews, 8 conversation comments):

| Fetch | REST | GraphQL |
|---|---|---|
| All review threads with comments | 2 requests, no `isResolved` | 1 point |
| Reviews + conversation comments | 2 requests | 1 point (or 0 extra if in the same query) |
| Changed files with patches | 4 requests, patches truncated for large files, 3,000-file cap | not available |
| Full refresh, everything above | ~9 requests | 1–2 points |

A full refresh costs about 0.04% of the hourly GraphQL budget. A single user
working on a single PR will not hit the primary limit even with aggressive
refreshing. The limit that can realistically be hit is the secondary
content-creation limit, when posting many replies at once.

## Pass 1: optimize for rate limits and cost

**P1-a. Diffs and file contents come from the local git clone.** Zero API calls,
no truncation, no file cap. The REST files endpoint would cost 4 requests per
refresh for this PR and truncates large patches.

**P1-b. Review threads are read with GraphQL.** This is the only API that
returns `isResolved`, and it is cheaper: 1 point instead of 2+ requests.

**P1-c. Reviews and conversation comments are read with GraphQL in the same
query as the threads.** They add no cost when they fit in the first page.

**P1-d. Change detection uses REST conditional requests.** Before a full
GraphQL refresh, send `GET /pulls/N`, `/pulls/N/comments` and
`/issues/N/comments` with the stored ETags. A 304 costs nothing, so polling is
free when nothing changed. Only fetch through GraphQL when something returned
200.

Known gap: resolving or unresolving a thread does not appear in any REST
payload, so the ETags do not change. A periodic GraphQL sweep is still needed
to catch resolution changes made by other people.

**P1-e. Writes are GraphQL mutations, several per request using aliases.**
Replies are added to a pending review, so reviewers get one notification. Up to
about 20 replies go in one HTTP request. Writes are throttled to stay under 80
content-creating requests per minute.

**P1-f. Everything fetched is cached in SQLite.** Navigating the UI never calls
GitHub; only an explicit refresh does.

## Pass 2: revisit for developer experience

**P1-a (local git): kept.** It is also better for DX. It works offline, matches
what `git diff` shows in a terminal, and there's no pagination or truncation
code to write. The cost is that the user needs a local clone with the PR's
commits fetched. The app checks for the commits and gives the exact `git fetch`
command when they are missing.

**P1-b and P1-c (GraphQL reads): kept, simplified.** One fetch function returns
one `PullRequest` model built from GraphQL. Reviews and conversation comments
are paginated with the same helper as threads, rather than assuming they fit in
the first page. Every query also requests `rateLimit { cost remaining resetAt }`,
and the client logs it, so the cost of each call is measured, not estimated.

**P1-d (REST ETag polling): dropped.** It saves 1–2 points per refresh out of
5,000 per hour. It also cannot detect resolution changes, so GraphQL is still
needed. That leaves two change-detection mechanisms that can disagree, two
clients, and ETag storage to keep correct. Instead, refresh runs the full
GraphQL fetch: on demand, and when the window regains focus, at most once per
30 seconds. At that rate the worst case is 120 refreshes × 2 points = 240
points per hour, about 5% of the budget.

Revisit this if reviewr starts watching many PRs in the background. At around
50 PRs refreshed every minute, the ETag probe would be worth adding.

**P1-e (aliased batch mutations): changed to one mutation per request.** With
aliases, a partial failure returns some results and some errors in one response,
and the code has to work out which replies were posted. One mutation per request
means each reply has a clear success or failure, and retrying a failed one is
simple. The rate-limit concern is handled separately: replies are sent one at a
time with a 1-second gap. 88 replies take about 90 seconds and use 440
secondary points spread across that time, well under every limit.

**P1-f (SQLite cache): kept.** It also helps DX: the UI loads instantly, and
the cached JSON is easy to inspect when something looks wrong.

**Additional DX decisions** that have no rate-limit impact:

- **Auth reuses `gh auth token`.** No token setup or storage in reviewr.
  `GITHUB_TOKEN` overrides it if set.
- **HTTP is done with `httpx`, not by shelling out to `gh api`.** Typed
  responses, real exceptions, and easy testing with a mock transport. `gh` is
  used only to get the token.
- **GraphQL queries live in `.graphql` files** next to the client, so they can
  be pasted into GitHub's GraphQL Explorer as-is.

## Final decisions

| ID | Decision | Chosen on the basis of |
|---|---|---|
| D1 | Diffs and file contents come from local git, never the API | Rate limit and DX (both point the same way) |
| D2 | Review threads are read with GraphQL | Capability (`isResolved` exists only in GraphQL); also cheaper |
| D3 | Reviews and conversation comments are read with GraphQL, using the same pagination helper as threads | DX (one API, one model); cost difference is negligible |
| D4 | No REST ETag polling; refresh is a full GraphQL fetch, on demand or on focus, at most every 30 s | DX. Rate-limit cost was measured at ~2 points per refresh and judged acceptable |
| D5 | Replies and resolve/unresolve are GraphQL mutations; replies go into a pending review | Capability (resolve exists only in GraphQL) and DX (one API for writes) |
| D6 | One mutation per request, sent sequentially at most one per second | DX for the one-per-request part; rate limit (secondary content-creation limit) for the throttle |
| D7 | Every query requests `rateLimit` and the client logs cost and remaining budget | DX (cost is visible instead of guessed) |
| D8 | All GitHub data is cached in SQLite; UI navigation never calls GitHub | Rate limit and DX |
| D9 | Token from `gh auth token`, `GITHUB_TOKEN` overrides | DX |
| D10 | `httpx` client, queries in `.graphql` files | DX |

REST is not used in the current design. It becomes useful again only for D4's
background-polling case described above.

[rest]: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
[gql]: https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api
