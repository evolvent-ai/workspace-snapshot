#!/bin/sh
set -eu
root=${PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}
case "$(uname -m)" in
  arm64|aarch64) platform=darwin-arm64 ;;
  x86_64|amd64) platform=darwin-amd64 ;;
  *) echo "Unsupported macOS architecture" >&2; exit 1 ;;
esac
exec "$root/bin/$platform/session-archive" "$@"
