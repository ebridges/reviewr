#!/usr/bin/env node
// Versioning. The version lives in five files, which this script keeps in step:
// package.json, frontend/package.json, desktop/package.json (plus their lockfiles),
// backend/pyproject.toml (plus uv.lock) and backend/reviewr/__init__.py.
//
//   npm run release -- patch|minor|major|X.Y.Z[-pre] [--no-push]
//       Sets the new version, commits "chore(release): vX.Y.Z", tags vX.Y.Z and pushes
//       both. The tag starts .github/workflows/release.yml, which builds the app and
//       publishes the release. Giving the current version tags it without a commit
//       (for the first release).
//
//   node scripts/release.mjs --check vX.Y.Z
//       Exits non-zero unless every version file says X.Y.Z. Used by the release workflow.

import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SEMVER = /^(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?$/;

const JSON_FILES = ["package.json", "frontend/package.json", "desktop/package.json"];
const LOCK_FILES = ["frontend/package-lock.json", "desktop/package-lock.json"];
const PYPROJECT = "backend/pyproject.toml";
const PY_INIT = "backend/reviewr/__init__.py";
const PYPROJECT_VERSION = /^version = "([^"]+)"/m;
const PY_INIT_VERSION = /^__version__ = "([^"]+)"/m;

const read = (f) => readFileSync(path.join(ROOT, f), "utf8");
const write = (f, text) => writeFileSync(path.join(ROOT, f), text);
// Returns stdout, trimmed; "" when stdout goes to the terminal (stdio: "inherit").
const run = (cmd, args, opts = {}) =>
  (execFileSync(cmd, args, { cwd: ROOT, encoding: "utf8", stdio: ["ignore", "pipe", "inherit"], ...opts }) ?? "").trim();

function fail(message) {
  console.error(`release: ${message}`);
  process.exit(1);
}

function versions() {
  const found = {};
  for (const f of JSON_FILES) found[f] = JSON.parse(read(f)).version;
  for (const f of LOCK_FILES) {
    const lock = JSON.parse(read(f));
    found[f] = lock.version;
    found[`${f} (packages[""])`] = lock.packages?.[""]?.version;
  }
  found[PYPROJECT] = read(PYPROJECT).match(PYPROJECT_VERSION)?.[1];
  found[PY_INIT] = read(PY_INIT).match(PY_INIT_VERSION)?.[1];
  return found;
}

function check(tag) {
  const want = tag.replace(/^v/, "");
  if (!SEMVER.test(want)) fail(`${tag} isn't a vX.Y.Z tag.`);
  const wrong = Object.entries(versions()).filter(([, v]) => v !== want);
  if (wrong.length) {
    fail(`tag ${tag} doesn't match:\n${wrong.map(([f, v]) => `  ${f}: ${v}`).join("\n")}`);
  }
  console.log(`All version files say ${want}.`);
}

function bump(current, how) {
  if (SEMVER.test(how)) return how;
  const m = current.match(SEMVER);
  if (!m) fail(`the current version ${current} isn't X.Y.Z.`);
  const [maj, min, pat] = m.slice(1, 4).map(Number);
  if (how === "major") return `${maj + 1}.0.0`;
  if (how === "minor") return `${maj}.${min + 1}.0`;
  if (how === "patch") return m[4] ? `${maj}.${min}.${pat}` : `${maj}.${min}.${pat + 1}`;
  fail(`expected patch, minor, major or a version like 1.2.3, not "${how}".`);
}

function setVersion(version) {
  for (const f of JSON_FILES) {
    const data = JSON.parse(read(f));
    data.version = version;
    write(f, JSON.stringify(data, null, 2) + "\n");
  }
  for (const f of LOCK_FILES) {
    const data = JSON.parse(read(f));
    data.version = version;
    if (data.packages?.[""]) data.packages[""].version = version;
    write(f, JSON.stringify(data, null, 2) + "\n");
  }
  write(PYPROJECT, read(PYPROJECT).replace(PYPROJECT_VERSION, `version = "${version}"`));
  write(PY_INIT, read(PY_INIT).replace(PY_INIT_VERSION, `__version__ = "${version}"`));
  run("uv", ["lock", "--quiet"], { cwd: path.join(ROOT, "backend") });
}

function release(how, push) {
  if (run("git", ["status", "--porcelain"])) fail("the working tree has uncommitted changes.");
  const branch = run("git", ["branch", "--show-current"]);
  if (branch !== "main") fail(`releases are made from main, and this is ${branch || "a detached HEAD"}.`);
  run("git", ["fetch", "--quiet", "--tags", "origin", "main"]);
  if (run("git", ["rev-list", "--count", "HEAD..origin/main"]) !== "0") {
    fail("main is behind origin/main. Pull first.");
  }

  const current = JSON.parse(read("package.json")).version;
  const version = bump(current, how);
  const tag = `v${version}`;
  if (run("git", ["tag", "--list", tag])) fail(`${tag} already exists.`);

  if (version !== current) {
    setVersion(version);
    run("git", ["add", ...JSON_FILES, ...LOCK_FILES, PYPROJECT, PY_INIT, "backend/uv.lock"]);
    run("git", ["commit", "--quiet", "-m", `chore(release): ${tag}`]);
    console.log(`Version ${current} -> ${version}, committed.`);
  } else {
    console.log(`Tagging the current version, ${version}.`);
  }
  check(tag);
  run("git", ["tag", "-a", tag, "-m", `reviewr ${version}`]);
  console.log(`Tagged ${tag}.`);

  if (!push) {
    console.log(`Not pushed. To publish: git push --atomic origin main ${tag}`);
    return;
  }
  run("git", ["push", "--atomic", "origin", "main", tag], { stdio: "inherit" });
  console.log(`Pushed. The release workflow builds and publishes ${tag}.`);
}

const args = process.argv.slice(2);
if (args[0] === "--check") {
  if (!args[1]) fail("usage: release.mjs --check vX.Y.Z");
  check(args[1]);
} else if (args[0] && !args[0].startsWith("-")) {
  release(args[0], !args.includes("--no-push"));
} else {
  fail("usage: npm run release -- patch|minor|major|X.Y.Z [--no-push]");
}
