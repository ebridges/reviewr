"""Work out a repository's dependency installs and checks from its files.

Used by workspace setup when a repo has none configured. Everything here reads the
files at the PR's head commit (a list of paths and a reader), so it's the branch under
review that decides, not your clone's checkout.

Installs (config.EnvConfig), one per lockfile:
  npm (package-lock.json), pnpm (pnpm-lock.yaml), Yarn (yarn.lock), Bun (bun.lock[b]),
  uv (uv.lock), Poetry (poetry.lock), and requirements*.txt without a lockfile.
Checks (config.CheckConfig), from the dependencies those installs bring in:
  ESLint, Jest and Vitest for JavaScript/TypeScript; Ruff, Black and pytest for Python.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Callable

from reviewr.config import CheckConfig, EnvConfig

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
