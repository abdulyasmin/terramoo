#!/usr/bin/env bash
# Build ToastStunt out of tree into .build/ and prepare a working ToastCore DB
# in .run/ with a wizard `wiz`/`wiz` and a non-wizard programmer `tester`/`tester`.
# Idempotent: rebuilds only what changed, prepares the DB only if missing.
#   ./setup.sh           build + prepare if missing
#   ./setup.sh --reset   also re-prepare base.db and reset moo.db from it
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="${TOASTSTUNT_SRC:-$HERE/.src/toaststunt}"
CORE_DB="${TOASTCORE_DB:-$HERE/.src/toastcore/toastcore.db}"
PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"   # builds are per platform: the tree may be synced between machines
BUILD="$HERE/.build/$PLATFORM"
RUN="$HERE/.run"
RESET=0
[[ "${1:-}" == "--reset" ]] && RESET=1

# --- sources: clone upstream unless TOASTSTUNT_SRC / TOASTCORE_DB point elsewhere
if [[ -z "${TOASTSTUNT_SRC:-}" && ! -d "$SRC/.git" ]]; then
  git clone --quiet --depth 1 https://github.com/lisdude/toaststunt "$SRC"
fi
if [[ -z "${TOASTCORE_DB:-}" && ! -f "$CORE_DB" ]]; then
  git clone --quiet --depth 1 https://github.com/lisdude/toastcore "$(dirname "$CORE_DB")"
fi

[[ -f "$SRC/CMakeLists.txt" ]] || { echo "no ToastStunt source at $SRC" >&2; exit 1; }
[[ -f "$CORE_DB" ]] || { echo "no ToastCore DB at $CORE_DB" >&2; exit 1; }

# --- dependencies. macOS: Homebrew (Apple's bison 2.3 is too old; use brew's).
# Elsewhere: check for what CMakeLists.txt requires and name the Debian packages.
if [[ "$(uname -s)" == Darwin ]] && command -v brew >/dev/null; then
  missing=()
  for f in cmake bison gperf nettle argon2 pcre2 openssl@3; do
    brew list --formula "$f" >/dev/null 2>&1 || missing+=("$f")
  done
  if ((${#missing[@]})); then
    echo "installing: ${missing[*]}"
    brew install "${missing[@]}"
  fi
  export PATH="$(brew --prefix bison)/bin:$PATH"
else
  missing=()
  for tool in cmake bison gperf perl cc c++ make pkg-config; do
    command -v "$tool" >/dev/null || missing+=("$tool")
  done
  for lib in nettle hogweed libargon2; do
    pkg-config --exists "$lib" 2>/dev/null || missing+=("$lib")
  done
  if ((${#missing[@]})); then
    echo "missing: ${missing[*]}" >&2
    echo "Debian/Ubuntu: sudo apt install cmake bison gperf perl build-essential pkg-config nettle-dev libargon2-dev" >&2
    echo "optional (more builtins): libpcre2-dev libcurl4-openssl-dev libaspell-dev libsqlite3-dev libssl-dev" >&2
    exit 1
  fi
fi

# --- build (out of tree; the source tree is never written to)
if [[ ! -f "$BUILD/CMakeCache.txt" ]]; then
  cmake -S "$SRC" -B "$BUILD" -DCMAKE_BUILD_TYPE=Release
fi
cmake --build "$BUILD" -j"$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"
[[ -x "$BUILD/moo" ]] || { echo "build produced no moo binary" >&2; exit 1; }

# --- working DB
mkdir -p "$RUN"
if [[ -f "$RUN/moo.pid" ]] && kill -0 "$(cat "$RUN/moo.pid")" 2>/dev/null; then
  if ((RESET)); then echo "server is running; ./stop.sh first" >&2; exit 1; fi
fi

if ((RESET)) || [[ ! -f "$RUN/base.db" ]]; then
  echo "preparing base.db from $CORE_DB"
  cp "$CORE_DB" "$RUN/prep.in.db"
  rm -f "$RUN/prep.out.db"
  # Emergency wizard mode reads commands on stdin, runs them as #2, and
  # `quit` dumps the result to prep.out.db. No network involved.
  "$BUILD/moo" -e "$RUN/prep.in.db" "$RUN/prep.out.db" \
      < "$HERE/prepare.moo" > "$RUN/prep.log" 2>&1
  if ! grep -q '=> {{#[0-9]*, "wiz", 1, 1, #[0-9]*, 100000}, {#[0-9]*, "tester", 0, 1, #[0-9]*, 100000}}' "$RUN/prep.log" \
     || [[ ! -s "$RUN/prep.out.db" ]]; then
    echo "DB preparation failed; see $RUN/prep.log" >&2; exit 1
  fi
  mv "$RUN/prep.out.db" "$RUN/base.db"
  rm -f "$RUN/prep.in.db"
  grep '^=> {{' "$RUN/prep.log"
fi

if ((RESET)) || [[ ! -f "$RUN/moo.db" ]]; then
  cp "$RUN/base.db" "$RUN/moo.db"
  rm -f "$RUN/moo.db.new"
  echo "working DB reset: $RUN/moo.db"
fi
echo "ready: ./start.sh"
