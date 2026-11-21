#!/bin/sh
# Build the downloadable macOS app (Apple Silicon) into desktop/release/:
#   reviewr-<version>-arm64.dmg and reviewr-<version>-arm64.zip (ad-hoc signed only, no
#   Developer ID; see desktop/adhoc-sign.cjs).
#
# Steps: the frontend (Vite), the backend as a standalone binary (PyInstaller, no Python
# needed to run it), then the Electron app around both (electron-builder).
# Run `npm run setup` first. Used by the release workflow too.
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

if [ "$(uname -s)" != "Darwin" ] || [ "$(uname -m)" != "arm64" ]; then
  echo "package.sh builds the Apple Silicon macOS app and has to run on one." >&2
  exit 1
fi

echo "==> frontend"
npm --prefix frontend run build

echo "==> backend"
rm -rf backend/build backend/dist
(cd backend && uv sync --group build --quiet && uv run --group build pyinstaller --noconfirm --log-level WARN packaging/reviewr.spec)
backend/dist/reviewr/reviewr --version

echo "==> app"
rm -rf desktop/release
npm --prefix desktop run dist

ls -lh desktop/release/*.dmg desktop/release/*.zip
