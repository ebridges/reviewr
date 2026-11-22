"""Find the Node and Python versions a repository asks for, and an install of each.

Node: the version comes from .nvmrc, .node-version, .tool-versions or package.json's
engines.node. reviewr looks for a matching install from nvm, fnm, Volta, mise, asdf or
Homebrew (or `node` on PATH). If none matches and nvm, fnm or mise is available, it
installs one with that tool. The result is a bin folder to put first on PATH.

Python: the version comes from .python-version, .tool-versions, runtime.txt or
pyproject.toml's requires-python. uv finds a matching interpreter, or installs one;
dependency installs then pass it to uv with --python.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

Reader = Callable[[str], bytes | None]
Version = tuple[int, int, int]

INSTALL_TIMEOUT_S = 900


@dataclass
class Toolchain:
    name: str  # "node" or "python"
    requested: str | None  # what the repo asks for, e.g. "22" or ">=3.11"
    source: str | None  # the file that asked
    version: str | None  # the version found
    bin_dir: str | None  # put first on PATH (Node only)
    note: str = ""  # anything you should know, e.g. no exact match
    ok: bool = True


def _text(read: Reader, path: str) -> str | None:
    data = read(path)
    return data.decode(errors="replace").strip() if data is not None else None


def _tool_versions(read: Reader, tool: str) -> str | None:
    for line in (_text(read, ".tool-versions") or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == tool:
            return parts[1]
    return None


def _parse(v: str) -> Version | None:
    m = re.match(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", v.strip())
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0)) if m else None


# Node

def node_request(read: Reader) -> tuple[str | None, str | None]:
    for f in (".nvmrc", ".node-version"):
        if text := _text(read, f):
            return text.splitlines()[0].strip(), f
    if v := _tool_versions(read, "nodejs") or _tool_versions(read, "node"):
        return v, ".tool-versions"
    try:
        pkg = json.loads(_text(read, "package.json") or "{}")
    except ValueError:
        pkg = {}
    if isinstance(pkg, dict) and (eng := (pkg.get("engines") or {}).get("node")):
        return str(eng), "package.json engines"
    return None, None


def _node_installs() -> dict[Version, Path]:
    """Installed Node versions and their bin folders."""
    home = Path.home()
    patterns = [
        (home / ".nvm" / "versions" / "node", "v*", "bin"),
        (home / ".local" / "share" / "fnm" / "node-versions", "v*", "installation/bin"),
        (home / "Library" / "Application Support" / "fnm" / "node-versions", "v*", "installation/bin"),
        (home / ".volta" / "tools" / "image" / "node", "*", "bin"),
        (home / ".local" / "share" / "mise" / "installs" / "node", "*", "bin"),
        (home / ".asdf" / "installs" / "nodejs", "*", "bin"),
    ]
    found: dict[Version, Path] = {}
    for root, glob, sub in patterns:
        for d in root.glob(glob) if root.is_dir() else []:
            if (v := _parse(d.name)) and (d / sub / "node").exists():
                found.setdefault(v, d / sub)
    for brew in (Path("/opt/homebrew/opt"), Path("/usr/local/opt")):
        for d in brew.glob("node*") if brew.is_dir() else []:
            if (node := d / "bin" / "node").exists() and (v := _node_version(node)):
                found.setdefault(v, d / "bin")
    if (node := shutil.which("node")) and (v := _node_version(Path(node))):
        found.setdefault(v, Path(node).resolve().parent)
    return found


def _node_version(node: Path) -> Version | None:
    try:
        out = subprocess.run([str(node), "--version"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return _parse(out)


def node_matches(requested: str, v: Version) -> bool:
    r = requested.strip().lower()
    if r in ("node", "latest", "current", "stable") or r.startswith("lts"):
        return r.startswith("lts") is False or v[0] % 2 == 0  # LTS lines are even
    lows = re.findall(r">=?\s*v?(\d+)", r)
    highs = re.findall(r"<\s*v?(\d+)", r)
    if lows or highs:
        return all(v[0] >= int(lo) for lo in lows) and all(v[0] < int(hi) for hi in highs)
    want = _parse(r.lstrip("^~="))
    return bool(want) and v[0] == want[0]


def resolve_node(read: Reader, install: bool = True) -> Toolchain:
    requested, source = node_request(read)
    installs = _node_installs()
    if requested is None:
        if not installs:
            return Toolchain("node", None, None, None, None, "No Node.js found.", ok=False)
        v, bin_dir = max(installs.items())
        return Toolchain("node", None, None, ".".join(map(str, v)), str(bin_dir), "The repo doesn't pin a Node version.")
    exact = _parse(requested)
    if exact and requested.count(".") == 2 and exact in installs:
        return Toolchain("node", requested, source, ".".join(map(str, exact)), str(installs[exact]))
    matching = sorted(v for v in installs if node_matches(requested, v))
    if not matching and install and _install_node(requested):
        installs = _node_installs()
        matching = sorted(v for v in installs if node_matches(requested, v))
    if not matching:
        how = "nvm install, fnm install or mise use" if not install else "nvm, fnm or mise, then run setup again"
        return Toolchain("node", requested, source, None, None, f"No installed Node.js matches {requested}. Install it with {how}.", ok=False)
    best = matching[-1]
    note = "" if not exact or requested.count(".") < 2 or best == exact else f"Using {'.'.join(map(str, best))}, the closest installed version."
    return Toolchain("node", requested, source, ".".join(map(str, best)), str(installs[best]), note)


def _install_node(requested: str) -> bool:
    """Install a matching Node with whichever version manager you have."""
    spec = requested.lstrip("v^~=").split(" ")[0]
    if not re.fullmatch(r"[0-9.x*]+|lts/\*|lts/[a-z]+|node", spec):
        m = re.search(r"(\d+)", requested)
        if not m:
            return False
        spec = m.group(1)
    nvm = Path.home() / ".nvm" / "nvm.sh"
    if nvm.exists():
        argv = ["bash", "-c", 'source "$1" && nvm install "$2"', "nvm", str(nvm), spec]
    elif shutil.which("fnm"):
        argv = ["fnm", "install", spec]
    elif shutil.which("mise"):
        argv = ["mise", "install", f"node@{spec}"]
    else:
        return False
    try:
        return subprocess.run(argv, capture_output=True, timeout=INSTALL_TIMEOUT_S, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# Python

def python_request(read: Reader) -> tuple[str | None, str | None]:
    if text := _text(read, ".python-version"):
        return text.splitlines()[0].strip(), ".python-version"
    if v := _tool_versions(read, "python"):
        return v, ".tool-versions"
    if (text := _text(read, "runtime.txt")) and text.startswith("python-"):
        return text.removeprefix("python-").splitlines()[0], "runtime.txt"
    try:
        project = tomllib.loads(_text(read, "pyproject.toml") or "")
    except tomllib.TOMLDecodeError:
        project = {}
    if req := (project.get("project") or {}).get("requires-python"):
        return str(req), "pyproject.toml requires-python"
    return None, None


def resolve_python(read: Reader, install: bool = True) -> Toolchain:
    requested, source = python_request(read)
    if not shutil.which("uv"):
        return Toolchain("python", requested, source, None, None, "uv isn't installed (https://docs.astral.sh/uv/).", ok=False)
    spec = requested or "3"
    path = _uv_find(spec)
    if path is None and install:
        subprocess.run(["uv", "python", "install", spec], capture_output=True, timeout=INSTALL_TIMEOUT_S, check=False)
        path = _uv_find(spec)
    if path is None:
        return Toolchain("python", requested, source, None, None, f"No Python matches {spec}, and uv couldn't install one.", ok=False)
    out = subprocess.run([path, "--version"], capture_output=True, text=True, check=False).stdout
    version = out.split()[-1] if out.strip() else None
    return Toolchain("python", requested, source, version, None)


def _uv_find(spec: str) -> str | None:
    try:
        proc = subprocess.run(["uv", "python", "find", spec], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None if proc.returncode == 0 else None
