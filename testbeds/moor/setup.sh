#!/usr/bin/env bash
# Build a pinned mooR and prepare a LambdaCore world with test logins.
# Idempotent: each step is skipped when its result is already in place.
#   ./setup.sh           build (if needed) and provision (if needed)
#   ./setup.sh --reset   throw away .run/ (the world) and provision afresh
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
MOOR_REPO=https://github.com/rdaum/moor
# Tag 1.0.2 (2026-07-22), the latest release. Not main: on main (e.g. 279b286,
# 2026-09-22) command dispatch requires the verb's x flag, so LambdaCore's
# "rd" command verbs (;eval, @create, inventory...) answer "You can't do that."
# to every non-wizard.
MOOR_COMMIT=7caba6e5d850f33f155f842c311dbae328367af8
SRC=$HERE/.build/moor
RUN=$HERE/.run
PROFILE=release-fast   # mooR's own release profile for dev/quick-start builds
BIN=$SRC/target/$PROFILE/moor-daemon

if [ "${1:-}" = "--reset" ]; then
  "$HERE/stop.sh" >/dev/null || true
  rm -rf "$RUN"
fi

# 1. source at the pinned commit
mkdir -p "$HERE/.build"
if [ ! -d "$SRC/.git" ]; then
  git clone --quiet "$MOOR_REPO" "$SRC"
fi
if [ "$(git -C "$SRC" rev-parse HEAD)" != "$MOOR_COMMIT" ]; then
  git -C "$SRC" cat-file -e "$MOOR_COMMIT^{commit}" 2>/dev/null || git -C "$SRC" fetch --quiet origin
  git -C "$SRC" -c advice.detachedHead=false checkout --quiet "$MOOR_COMMIT"
fi

# 2. binaries (cargo is itself incremental; the stamp skips even that)
STAMP=$HERE/.build/built-$MOOR_COMMIT
if [ ! -x "$BIN" ] || [ ! -f "$STAMP" ]; then
  echo "building mooR $MOOR_COMMIT ($PROFILE); this takes a while..."
  (cd "$SRC" && cargo build --profile "$PROFILE" -p moor-daemon -p moor-telnet-host -p moor-web-host)
  rm -f "$HERE"/.build/built-*; touch "$STAMP"
fi

# 3. world: import lambda-moor on first start, then add the test logins
if [ -f "$RUN/provisioned" ]; then
  echo "world already provisioned ($RUN/data); ./setup.sh --reset to rebuild it"
  exit 0
fi
"$HERE/stop.sh" >/dev/null || true
rm -rf "$RUN"; mkdir -p "$RUN"
"$HERE/start.sh"
trap '"$HERE/stop.sh" >/dev/null || true' EXIT
python3 "$HERE/provision.py"
date >"$RUN/provisioned"
echo "provisioned: Wizard/wizard (wizard), tester/tester (programmer)"
