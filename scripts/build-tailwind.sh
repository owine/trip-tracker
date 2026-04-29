#!/usr/bin/env bash
# Build static/tailwind.css from input.css using the standalone Tailwind CLI.
# Downloads the platform-appropriate binary if not present.
set -euo pipefail

VERSION="v4.2.4"
BIN_DIR=".tailwind"
BIN="$BIN_DIR/tailwindcss"

mkdir -p "$BIN_DIR"

if [[ ! -x "$BIN" ]]; then
  case "$(uname -s)-$(uname -m)" in
    Linux-x86_64)   ASSET="tailwindcss-linux-x64" ;;
    Linux-aarch64)  ASSET="tailwindcss-linux-arm64" ;;
    Darwin-arm64)   ASSET="tailwindcss-macos-arm64" ;;
    Darwin-x86_64)  ASSET="tailwindcss-macos-x64" ;;
    *) echo "Unsupported platform $(uname -s)-$(uname -m)"; exit 1 ;;
  esac
  echo "Downloading Tailwind $VERSION ($ASSET)..."
  curl -fsSL -o "$BIN" "https://github.com/tailwindlabs/tailwindcss/releases/download/${VERSION}/${ASSET}"
  chmod +x "$BIN"
fi

"$BIN" -i src/trip_tracker/static/input.css \
       -o src/trip_tracker/static/tailwind.css \
       --minify
