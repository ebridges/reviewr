import { useQuery } from "@tanstack/react-query";
import { Header, STEPS } from "./components/Header";
import { api, type PRView } from "./api";
import { prHref, usePR, useRoute, type Route } from "./lib";
import { CI } from "./pages/CI";
import { Fix } from "./pages/Fix";
import { Run } from "./pages/Run";
import { Merge } from "./pages/Merge";
import { Overview } from "./pages/Overview";
import { Plan } from "./pages/Plan";
import { PRList } from "./pages/PRList";
import { Respond } from "./pages/Respond";
import { Settings } from "./pages/Settings";
import { Setup } from "./pages/Setup";
import { Triage } from "./pages/Triage";

export default function App() {
  const route = useRoute();
  if (!route.ref) {
    return (
      <div className="app">
        <Header route={route} />
        {route.page === "settings" ? <Settings /> : <PRList />}
      </div>
    );
  }
  return <PRPages route={route} />;
}

function PRPages({ route }: { route: Route }) {
  const ref = route.ref!;
  // Whether the repo has a clone yet; until it does, the PR opens on workspace setup.
  const setup = useQuery({
    queryKey: ["setup", ref.owner, ref.repo, ref.number],
    queryFn: () => api.setup(ref),
    refetchInterval: (q) => (q.state.data?.running ? 1500 : false),
  });
  const s = setup.data;
  const inSetup = !!s && (!!s.needed || s.running);
  const pr = usePR(ref, !!s && !inSetup);

  if (setup.error) {
    return <div className="app"><Header route={route} /><div className="banner error">{(setup.error as Error).message}</div></div>;
  }
  if (!s) {
    return <div className="app"><Header route={route} /><div className="pane-msg muted">Loading…</div></div>;
  }
  if (inSetup) {
    return (
      <div className="app">
        <Header route={route} />
        <Setup prRef={ref} setup={setup} />
      </div>
    );
  }

  return (
    <div className="app">
      <Header route={route} view={pr.data} />
      {pr.error && <div className="banner error">{(pr.error as Error).message}</div>}
      {!pr.data && !pr.error && <div className="pane-msg muted">Loading the pull request from GitHub…</div>}
      {pr.data && (
        <>
          {route.page === "overview" && <Overview ctx={pr} />}
          {route.page === "triage" && <Triage ctx={pr} arg={route.arg} />}
          {route.page === "plan" && <Plan ctx={pr} />}
          {route.page === "fix" && <Fix ctx={pr} arg={route.arg} />}
          {route.page === "respond" && <Respond ctx={pr} />}
          {route.page === "merge" && <Merge ctx={pr} arg={route.arg} />}
          {route.page === "ci" && <CI ctx={pr} arg={route.arg} />}
          {route.page === "run" && <Run ctx={pr} />}
          <NextStep route={route} view={pr.data} />
        </>
      )}
    </div>
  );
}

export type PRContext = ReturnType<typeof usePR>;

/** Lower-right button to the next workflow step. Skips steps with nothing to do: once
 *  every thread is decided and none is marked Fix, Plan and Fix are skipped; if nothing
 *  needs a reply either, the PR is done. Respond is last, so it has no button. */
function NextStep({ route, view }: { route: Route; view: PRView }) {
  const i = STEPS.findIndex((s) => s.page === route.page);
  if (i < 0 || !route.ref || route.page === "respond") return null;
  const open = view.pr.threads.filter((t) => !t.is_resolved);
  const decisions = open.map((t) => view.state.decisions[t.id]?.decision);
  const allDecided = open.length > 0 && decisions.every(Boolean);
  const anyFix = decisions.includes("fix");
  const anyReply = decisions.some((d) => d && d !== "no_action");

  let href = prHref(route.ref, STEPS[i + 1].page);
  let label = `Next: ${STEPS[i + 1].label}`;
  if (allDecided && !anyFix && (route.page === "triage" || route.page === "plan")) {
    if (anyReply) {
      href = prHref(route.ref, "respond");
      label = "Next: Respond (nothing to fix)";
    } else {
      href = "#/";
      label = "Done: back to all pull requests";
    }
  }
  return (
    <a className="btn primary next-step" href={href}>
      {label} <span aria-hidden="true">→</span>
    </a>
  );
}
