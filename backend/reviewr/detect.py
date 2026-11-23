"""Work out a repository's dependency installs and checks from its files.

Used by workspace setup when a repo has none configured. Everything here reads the
files at the PR's head commit (a list of paths and a reader), so it's the branch under
review that decides, not your clone's checkout.

Installs (config.EnvConfig), one per lockfile:
  npm (package-lock.json), pnpm (pnpm-lock.yaml), Yarn (yarn.lock), Bun (bun.lock[b]),
  uv (uv.lock), Poetry (poetry.lock), and requirements*.txt without a lockfile.
Checks (config.CheckConfig), from the dependencies those installs bring in:
  ESLint, Jest and Vitest for JavaScript/TypeScript; Ruff, Black and pytest for Python.
Running the app (config.RunConfig, see detect_run): services from .claude/launch.json, a
  Procfile or package.json dev/start scripts; Docker Compose files; env files; and
  migration/seed commands. These are proposals: you save them in Settings before reviewr
  runs anything.
"""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from collections.abc import Callable

from reviewr.config import (
    CheckConfig,
    ComposeConfig,
    EnvConfig,
    RunConfig,
    ServiceConfig,
    SetupCommand,
)

Reader = Callable[[str], bytes | None]

JS_LOCKS = {"package-lock.json": "npm", "pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "bun.lock": "bun", "bun.lockb": "bun"}
PY_LOCKS = ("uv.lock", "poetry.lock")
SKIP_DIRS = ("node_modules/", ".venv/", "venv/", "vendor/", "dist/", "build/", "site-packages/")

JS_EXT = ("*.ts", "*.tsx", "*.js", "*.jsx", "*.mjs", "*.cjs")


def _skip(path: str) -> bool:
    return any(f"/{d}" in f"/{path}" for d in SKIP_DIRS)


def _join(d: str, name: str) -> str:
    return posixpath.join(d, name) if d else name


def _label(base: str, d: str) -> str:
    return f"{base} ({d})" if d else base


def _text(read: Reader, path: str) -> str:
    data = read(path)
    return data.decode(errors="replace") if data else ""


def detect_envs(files: list[str], read: Reader, python: str | None = None) -> tuple[list[EnvConfig], list[str]]:
    """Installs for the repo, and notes about lockfiles that were skipped."""
    present = set(files)
    envs: list[EnvConfig] = []
    notes: list[str] = []
    js_dirs: set[str] = set()
    for f in sorted(files):
        name, d = posixpath.basename(f), posixpath.dirname(f)
        if _skip(f) or name not in JS_LOCKS or d in js_dirs or _join(d, "package.json") not in present:
            continue
        js_dirs.add(d)
        tool = JS_LOCKS[name]
        where = d or "."
        extra = [p for p in (_join(d, ".npmrc"), ".npmrc", ".nvmrc", _join(d, ".nvmrc"), _join(d, ".yarnrc.yml"))
                 if p in present]
        workspace_files = []
        if tool == "pnpm" and _join(d, "pnpm-workspace.yaml") in present:
            workspace_files = [p for p in files if p.endswith("package.json") and not _skip(p) and p.startswith(d)]
            workspace_files.append(_join(d, "pnpm-workspace.yaml"))
        elif '"workspaces"' in _text(read, _join(d, "package.json")):
            workspace_files = [p for p in files if p.endswith("package.json") and not _skip(p) and p.startswith(d)]
        env_files = sorted({_join(d, "package.json"), f, *extra, *workspace_files})
        modules = _join(d, "node_modules")
        seed, seeded = [], None
        if tool == "npm":
            install = [["npm", "ci", "--prefix", where, "--no-audit", "--no-fund"]]
            seed, seeded = [modules], [["npm", "install", "--prefix", where, "--no-audit", "--no-fund", "--no-save"]]
        elif tool == "pnpm":
            install = [["pnpm", "install", "--frozen-lockfile", "--dir", where]]
        elif tool == "bun":
            install = [["bun", "install", "--frozen-lockfile", "--cwd", where]]
        else:  # yarn
            rc = _text(read, _join(d, ".yarnrc.yml"))
            if rc and "nodeLinker: node-modules" not in rc.replace('"', ""):
                notes.append(f"{f}: Yarn Plug'n'Play doesn't create node_modules, so it isn't installed per branch.")
                continue
            install = [["yarn", "--cwd", where, "install", "--immutable" if rc else "--frozen-lockfile"]]
        envs.append(EnvConfig(name=_label("node", d), files=env_files, install=install, provides={modules: modules},
                              seed=seed, install_seeded=seeded))

    py_dirs: set[str] = set()
    py_arg = ["--python", python] if python else []
    for f in sorted(files):
        name, d = posixpath.basename(f), posixpath.dirname(f)
        if _skip(f) or name not in PY_LOCKS or d in py_dirs or _join(d, "pyproject.toml") not in present:
            continue
        py_dirs.add(d)
        venv = _join(d, ".venv")
        extra = [p for p in (_join(d, ".python-version"), ".python-version") if p in present]
        files_ = sorted({_join(d, "pyproject.toml"), f, *extra})
        if name == "uv.lock":
            install = [["uv", "sync", "--frozen", "--no-install-project", "--project", d or ".", *py_arg]]
        else:
            install = [["env", "POETRY_VIRTUALENVS_IN_PROJECT=true", "poetry", "install", "--no-root", "--directory", d or "."]]
        envs.append(EnvConfig(name=_label("python", d), files=files_, install=install, provides={venv: venv}))
    # requirements files in folders without a Python lockfile
    req_dirs: dict[str, list[str]] = {}
    for f in files:
        name, d = posixpath.basename(f), posixpath.dirname(f)
        if not _skip(f) and re.fullmatch(r"requirements([-_.]?\w+)?\.txt", name) and d not in py_dirs:
            req_dirs.setdefault(d, []).append(f)
    for d, reqs in sorted(req_dirs.items()):
        reqs.sort(key=lambda r: ("dev" not in r and "test" not in r, r))
        venv = _join(d, ".venv")
        install = [["uv", "venv", *py_arg, venv], ["uv", "pip", "install", "--python", _join(venv, "bin/python")]]
        for r in reqs:
            install[1] += ["-r", r]
        envs.append(EnvConfig(name=_label("python", d), files=sorted(reqs), install=install, provides={venv: venv}))
    return envs, notes


def _js_deps(read: Reader, d: str) -> set[str]:
    try:
        pkg = json.loads(_text(read, _join(d, "package.json")) or "{}")
    except ValueError:
        return set()
    return {*(pkg.get("dependencies") or {}), *(pkg.get("devDependencies") or {})}


def detect_checks(envs: list[EnvConfig], files: list[str], read: Reader) -> list[CheckConfig]:
    checks: list[CheckConfig] = []
    present = set(files)
    for env in envs:
        (where,) = list(env.provides)  # node_modules or .venv
        d = posixpath.dirname(where)
        prefix = f"{d}/" if d else ""
        cwd = d or "."
        if where.endswith("node_modules"):
            deps = _js_deps(read, d)
            match = [prefix + ext for ext in JS_EXT]
            if "eslint" in deps:
                checks.append(CheckConfig(name=_label("ESLint", d), kind="lint", match=match, cwd=cwd,
                                          run=["node_modules/.bin/eslint", "--max-warnings", "0", "--no-warn-ignored", "{files}"]))
            if "jest" in deps:
                checks.append(CheckConfig(name=_label("Jest", d), kind="test", match=match, cwd=cwd,
                                          discover=["node_modules/.bin/jest", "--listTests", "--findRelatedTests", "{files}"],
                                          run=["node_modules/.bin/jest", "--ci", "{tests}"]))
            elif "vitest" in deps:
                # Vitest runs the tests related to the changed files itself.
                checks.append(CheckConfig(name=_label("Vitest (related tests)", d), kind="lint", match=match, cwd=cwd,
                                          run=["node_modules/.bin/vitest", "related", "--run", "--passWithNoTests", "{files}"]))
        else:
            text = " ".join(_text(read, f) for f in env.files).lower()
            pyproject = _text(read, _join(d, "pyproject.toml"))
            match = [prefix + "*.py"]
            if "ruff" in text or "[tool.ruff" in pyproject or _join(d, "ruff.toml") in present:
                checks.append(CheckConfig(name=_label("Ruff", d), kind="lint", match=match, cwd=cwd,
                                          run=[".venv/bin/ruff", "check", "{files}"]))
            if re.search(r"\bblack\b", text) or "[tool.black" in pyproject:
                checks.append(CheckConfig(name=_label("Black", d), kind="lint", match=match, cwd=cwd,
                                          run=[".venv/bin/black", "--check", "--diff", "--quiet", "{files}"]))
            if "pytest" in text:
                checks.append(CheckConfig(name=_label("pytest", d), kind="test", match=match, cwd=cwd, discover="python",
                                          run=[".venv/bin/python", "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", "{tests}"]))
    return checks


# Running the app

COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
# Ports dev servers use when a script doesn't pass one.
_DEFAULT_PORTS = (("vite", 5173), ("next", 3000), ("react-scripts", 3000), ("astro", 4321), ("nuxt", 3000),
                  ("remix", 3000), ("ng serve", 4200), ("webpack serve", 8080), ("webpack-dev-server", 8080))
_PORT_ARG = re.compile(r"(?:--port|-p|PORT=)[ =]?(\d{2,5})\b")


def _js_run(tool: str, script: str, where: str) -> list[str]:
    """argv that runs a package.json script with the package's own manager."""
    if tool == "npm":
        return ["npm", "run", script, "--prefix", where]
    if tool == "pnpm":
        return ["pnpm", "--dir", where, "run", script]
    if tool == "bun":
        return ["bun", "run", "--cwd", where, script]
    return ["yarn", "--cwd", where, "run", script]


def _guess_port(command: str) -> int | None:
    if m := _PORT_ARG.search(command):
        return int(m.group(1))
    return next((port for name, port in _DEFAULT_PORTS if name in command), None)


def _launch_json(read: Reader) -> tuple[list[ServiceConfig], list[str]]:
    """Services from .claude/launch.json (Claude Code's format: runtimeExecutable,
    runtimeArgs, port, cwd)."""
    text = _text(read, ".claude/launch.json")
    if not text:
        return [], []
    try:
        data = json.loads(text)
    except ValueError:
        return [], [".claude/launch.json isn't valid JSON, so it was skipped."]
    out = []
    for c in data.get("configurations") or []:
        exe = c.get("runtimeExecutable")
        if not isinstance(exe, str) or not exe:
            continue
        args = [str(a) for a in c.get("runtimeArgs") or []]
        port = c.get("port") if isinstance(c.get("port"), int) else None
        env = {str(k): str(v) for k, v in (c.get("env") or {}).items()}
        out.append(ServiceConfig(name=str(c.get("name") or exe), cwd=str(c.get("cwd") or "."), run=[exe, *args],
                                 env=env, port=port))
    return out, ([".claude/launch.json: services from its configurations."] if out else [])


def _procfile(read: Reader) -> tuple[list[ServiceConfig], list[str]]:
    out = []
    for line in _text(read, "Procfile").splitlines():
        name, sep, cmd = line.partition(":")
        if not sep or not name.strip() or name.strip().startswith("#") or not cmd.strip():
            continue
        try:
            argv = shlex.split(cmd)
        except ValueError:
            continue
        if name.strip() == "release":
            continue  # Heroku's release phase; usually migrations, handled as setup
        out.append(ServiceConfig(name=name.strip(), run=argv, port=_guess_port(cmd)))
    return out, (["Procfile: one service per process type."] if out else [])


def _package_scripts(files: list[str], read: Reader) -> list[tuple[str, str, dict[str, str]]]:
    """(dir, package manager, scripts) for each package with a JS lockfile."""
    present, seen, out = set(files), set(), []
    for f in sorted(files):
        name, d = posixpath.basename(f), posixpath.dirname(f)
        if _skip(f) or name not in JS_LOCKS or d in seen or _join(d, "package.json") not in present:
            continue
        seen.add(d)
        try:
            pkg = json.loads(_text(read, _join(d, "package.json")) or "{}")
        except ValueError:
            continue
        scripts = pkg.get("scripts") or {}
        out.append((d, JS_LOCKS[name], {k: v for k, v in scripts.items() if isinstance(v, str)}))
    return out


def _env_files(files: list[str], read: Reader) -> list[str]:
    """.env files an app reads but git doesn't have: next to an .env.example or
    .env.sample, or named in a .gitignore at the same level."""
    present, out = set(files), []
    dirs = sorted({posixpath.dirname(f) for f in files if not _skip(f)})
    for d in dirs:
        target = _join(d, ".env")
        if target in present:
            continue  # committed; nothing to copy
        examples = any(_join(d, n) in present for n in (".env.example", ".env.sample", ".env.template"))
        ignored = re.search(r"^/?\.env\b", _text(read, _join(d, ".gitignore")), re.MULTILINE)
        if examples or (ignored and d == ""):
            out.append(target)
    return out


def _setup_commands(files: list[str], read: Reader, scripts: list[tuple[str, str, dict[str, str]]]) -> list[SetupCommand]:
    present, out = set(files), []
    for f in sorted(files):
        name, d = posixpath.basename(f), posixpath.dirname(f)
        if _skip(f):
            continue
        venv_bin = ".venv/bin/"
        if name == "alembic.ini":
            out.append(SetupCommand(name=_label("Migrations (Alembic)", d), cwd=d or ".", run=[venv_bin + "alembic", "upgrade", "head"]))
        elif name == "manage.py" and "django" in _text(read, f).lower():
            out.append(SetupCommand(name=_label("Migrations (Django)", d), cwd=d or ".", run=[venv_bin + "python", "manage.py", "migrate"]))
        elif f.endswith("prisma/schema.prisma"):
            root = posixpath.dirname(posixpath.dirname(f))
            if _join(root, "package.json") in present:
                out.append(SetupCommand(name=_label("Migrations (Prisma)", root), cwd=root or ".",
                                        run=["node_modules/.bin/prisma", "migrate", "deploy"]))
    for d, tool, s in scripts:
        for script in ("migrate", "db:migrate", "seed", "db:seed"):
            if script in s:
                out.append(SetupCommand(name=_label(f"npm script: {script}", d), run=_js_run(tool, script, d or ".")))
    return out


def detect_run(files: list[str], read: Reader) -> tuple[RunConfig, list[str]]:
    """A proposed way to run the app at this branch, and notes on where each part came
    from. Services come from the first of: .claude/launch.json, Procfile, package.json
    dev (or start) scripts."""
    notes: list[str] = []
    present = set(files)
    scripts = _package_scripts(files, read)
    services, why = _launch_json(read)
    if not services:
        services, why = _procfile(read) if "Procfile" in present else ([], [])
    if not services:
        for d, tool, s in scripts:
            script = "dev" if "dev" in s else "start" if "start" in s else None
            if script:
                services.append(ServiceConfig(name=_label(script, d), run=_js_run(tool, script, d or "."),
                                              port=_guess_port(s[script])))
        if services:
            why = ["package.json: the dev (or start) script of each package."]
    notes += why
    if not services:
        notes.append("No services found. Add the commands that start the app.")
    for svc in services:
        if svc.port is None:
            notes.append(f"{svc.name}: no port found. Add one so reviewr knows when it's ready and what to open.")
    # The one to open: a service named like the app's front end, else the first with a port.
    with_port = [svc for svc in services if svc.port]
    front = [svc for svc in with_port if re.search(r"\b(web|frontend|front|app|client|ui|site|www)\b", svc.name.lower())]
    if opener := (front or with_port or [None])[0]:
        opener.open = True

    compose = None
    if found := next((f for f in COMPOSE_FILES if f in present), None):
        compose = ComposeConfig(file=found)
        notes.append(f"{found}: started with Docker Compose before the services.")
        if re.search(r"image:\s*['\"]?(?:\S+/)?(postgres|postgis|mysql|mariadb|mongo|redis)", _text(read, found)):
            notes.append(
                f"{found} runs a database. If your .env points at a database other people use, add env overrides "
                "(e.g. its host and port) so the PR's migrations change this one instead."
            )
    env_files = _env_files(files, read)
    if env_files:
        notes.append(f"Copied from your clone when missing: {', '.join(env_files)}.")
    setup = _setup_commands(files, read, scripts)
    if setup:
        notes.append("Setup commands run in order before the services start; remove any you don't want run each time.")
    return RunConfig(env_files=env_files, compose=compose, setup=setup, services=services), notes
