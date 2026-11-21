// electron-builder afterPack hook: ad-hoc sign the app before the .dmg and .zip are made.
//
// The app isn't signed with a Developer ID (package.json sets mac.identity to null).
// Without any signature, Apple Silicon Macs report the downloaded app as damaged instead
// of offering to open it, because electron-builder's changes to the bundle invalidate
// the signature Electron ships with. An ad-hoc signature fixes that; Gatekeeper still
// blocks the first open until the quarantine flag is cleared (see the README).
const { execFileSync } = require("node:child_process");
const path = require("node:path");

exports.default = async function adhocSign(context) {
  if (context.electronPlatformName !== "darwin") return;
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  execFileSync("codesign", ["--force", "--deep", "--sign", "-", app], { stdio: "inherit" });
  execFileSync("codesign", ["--verify", "--deep", "--strict", app], { stdio: "inherit" });
};
