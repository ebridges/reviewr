// reviewr desktop app.
//
// Starts the Python backend (`uv run reviewr serve`) on a free local port, waits for it,
// and opens a window on it. The backend also serves the built frontend
// (frontend/dist), so the window loads everything from one local address.
// Quitting the app stops the backend.
//
// In the packaged app (electron-builder, see package.json) the backend is the PyInstaller
// build in Resources/backend/reviewr and the frontend is in Resources/ui. It reads its
// config from ~/.config/reviewr (config.toml and .env).
//
//   npm start     build the frontend, then open the app
//   npm run open  open the app with the frontend already built
//   npm run dev   hot reload: Vite dev server on :5173, backend with --reload on :8765
//
// --reviewr-route=#/pr/owner/repo/1/overview opens the window on that page. `reviewr open`
// passes it; if the app is already running, the running window switches to the page.
//
// REVIEWR_SMOKE=/path/to/shot.png opens the app, saves a screenshot of the window once
// it has loaded, and quits. Used to check that startup works. REVIEWR_SMOKE_ROUTE
// (e.g. "#/pr/owner/repo/1/overview") picks the page to screenshot.

const { app, BrowserWindow, dialog, nativeTheme, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const PACKAGED = app.isPackaged;
const DEV = !PACKAGED && process.env.REVIEWR_DEV === "1";
const CONFIG_DIR = path.join(os.homedir(), ".config", "reviewr");
// Made from build/icon.svg by scripts/make-icons.sh; icon.icns is for packaging.
// Electron's default icon is used if the file is missing.
const ICON_FILE = path.join(__dirname, "build", "icon.png");
const ICON = fs.existsSync(ICON_FILE) ? ICON_FILE : undefined;
const SMOKE = process.env.REVIEWR_SMOKE;
const children = [];
let logStream;

// The hash route from --reviewr-route, or "" if there is none or it isn't a plain route.
function routeFrom(argv) {
  const arg = argv.find((a) => a.startsWith("--reviewr-route="));
  const route = arg ? arg.slice("--reviewr-route=".length) : "";
  return /^#\/[A-Za-z0-9_.\-/]*$/.test(route) ? route : "";
}

// Apps started from Finder get a minimal PATH. Add the usual places for uv, gh, node
// and the claude CLI (used by the coding agent) so the backend finds them.
function childEnv() {
  const home = os.homedir();
  const extra = ["/opt/homebrew/bin", "/usr/local/bin", path.join(home, ".local/bin"), path.join(home, ".cargo/bin")];
  // REVIEWR_GUI: the backend adopts your login shell's environment at startup (PATH,
  // SSH_AUTH_SOCK for signed commits, ...); see backend/reviewr/shellenv.py.
  return { ...process.env, PATH: [...extra, process.env.PATH || ""].join(path.delimiter), PYTHONUNBUFFERED: "1", REVIEWR_GUI: "1" };
}

function log(line) {
  if (!logStream) {
    const file = path.join(app.getPath("userData"), "backend.log");
    logStream = fs.createWriteStream(file, { flags: "a" });
  }
  logStream.write(line);
}

function start(cmd, args, cwd, env = {}) {
  const child = spawn(cmd, args, { cwd, env: { ...childEnv(), ...env }, detached: true });
  child.stdout.on("data", (d) => log(d.toString()));
  child.stderr.on("data", (d) => log(d.toString()));
  child.on("exit", (code) => log(`\n[${cmd} exited with ${code}]\n`));
  children.push(child);
  return child;
}

function stopChildren() {
  for (const child of children) {
    if (child.exitCode === null) {
      try { process.kill(-child.pid, "SIGTERM"); } catch { /* already gone */ }
    }
  }
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

async function isUp(url) {
  try {
    const res = await fetch(url);
    return res.ok;
  } catch {
    return false;
  }
}

async function waitFor(url, seconds, child) {
  for (let i = 0; i < seconds * 4; i++) {
    if (child && child.exitCode !== null) return false;
    if (await isUp(url)) return true;
    await new Promise((r) => setTimeout(r, 250));
  }
  return false;
}

function logTail() {
  try {
    const text = fs.readFileSync(path.join(app.getPath("userData"), "backend.log"), "utf8");
    return text.slice(-2500);
  } catch {
    return "";
  }
}

// Returns the URL the window should load.
async function startServers() {
  if (DEV) {
    // Reuse servers that are already running (e.g. started from a terminal).
    if (!(await isUp("http://127.0.0.1:8765/api/health"))) {
      start("uv", ["run", "--directory", "backend", "reviewr", "serve", "--port", "8765", "--reload"], ROOT);
    }
    if (!(await isUp("http://localhost:5173/"))) {
      start("npm", ["--prefix", "frontend", "run", "dev"], ROOT);
    }
    const ok = (await waitFor("http://127.0.0.1:8765/api/health", 90)) && (await waitFor("http://localhost:5173/", 60));
    if (!ok) throw new Error("The dev servers didn't start.");
    return "http://localhost:5173/";
  }

  if (PACKAGED) {
    const port = await freePort();
    const bin = path.join(process.resourcesPath, "backend", "reviewr", "reviewr");
    // Run from the config folder so the backend's ./reviewr.toml and ./.env lookups find
    // nothing unexpected; it reads ~/.config/reviewr/config.toml and .env.
    fs.mkdirSync(CONFIG_DIR, { recursive: true });
    const backend = start(bin, ["serve", "--port", String(port)], CONFIG_DIR, {
      REVIEWR_UI_DIST: path.join(process.resourcesPath, "ui"),
    });
    const base = `http://127.0.0.1:${port}`;
    if (!(await waitFor(`${base}/api/health`, 90, backend))) {
      throw new Error("The backend didn't start.");
    }
    return `${base}/`;
  }

  if (!fs.existsSync(path.join(ROOT, "frontend", "dist", "index.html"))) {
    throw new Error("The frontend isn't built yet. Run `npm start` (which builds it) instead of `npm run open`.");
  }
  const port = await freePort();
  const backend = start("uv", ["run", "--directory", "backend", "reviewr", "serve", "--port", String(port)], ROOT);
  const base = `http://127.0.0.1:${port}`;
  if (!(await waitFor(`${base}/api/health`, 90, backend))) {
    throw new Error("The backend didn't start.");
  }
  return `${base}/`;
}

function createWindow(url, route = "") {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: "reviewr",
    icon: ICON,
    show: !SMOKE,
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#0d1117" : "#f4f4f5",
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  });
  const origin = new URL(url).origin;

  // Links to GitHub, Jira and the like open in your browser; the window only shows reviewr.
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    if (/^https?:/.test(target)) shell.openExternal(target);
    return { action: "deny" };
  });
  win.webContents.on("will-navigate", (event, target) => {
    if (new URL(target).origin !== origin) {
      event.preventDefault();
      if (/^https?:/.test(target)) shell.openExternal(target);
    }
  });

  if (SMOKE) {
    win.webContents.once("did-finish-load", () => {
      setTimeout(async () => {
        const image = await win.webContents.capturePage();
        fs.writeFileSync(SMOKE, image.toPNG());
        app.quit();
      }, 4000);
    });
  }
  // A new build of the frontend may be on disk; never show a cached older one.
  const start = SMOKE && process.env.REVIEWR_SMOKE_ROUTE ? process.env.REVIEWR_SMOKE_ROUTE : route;
  win.webContents.session.clearCache().finally(() => win.loadURL(url + start));
  return win;
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    const [win] = BrowserWindow.getAllWindows();
    if (win) {
      const route = routeFrom(argv);
      // routeFrom only accepts [A-Za-z0-9_.-/] after "#/", and JSON.stringify quotes it.
      if (route) win.webContents.executeJavaScript(`location.hash = ${JSON.stringify(route)}`);
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(async () => {
    app.setName("reviewr");
    if (process.platform === "darwin" && ICON) app.dock.setIcon(ICON);
    try {
      const url = await startServers();
      createWindow(url, routeFrom(process.argv));
      app.on("activate", () => {
        if (BrowserWindow.getAllWindows().length === 0) createWindow(url);
      });
    } catch (err) {
      const detail = `${err.message}\n\nBackend log (${path.join(app.getPath("userData"), "backend.log")}):\n\n${logTail()}`;
      if (SMOKE) {
        console.error(detail);
      } else {
        dialog.showErrorBox("reviewr couldn't start", detail);
      }
      stopChildren();
      app.exit(1);
    }
  });

  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", stopChildren);
  process.on("exit", stopChildren);
}
