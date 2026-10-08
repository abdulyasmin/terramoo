#!/usr/bin/env bash
# Build classic LambdaMOO 1.8.1 and prepare a LambdaCore working DB.
# Idempotent: re-running skips finished steps. `setup.sh --reset` rebuilds the
# working DB from the pristine core (the server build is kept).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/.build"          # downloads, shared by every platform
PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"   # builds are per platform: the tree may be synced between machines
OUT="$BUILD/$PLATFORM"        # the extracted source and the binary
RUN="$HERE/.run"
PORT="${MOO_PORT:-17002}"

SERVER_TGZ="LambdaMOO-1.8.1.tar.gz"
SERVER_SHA256="1c404855e5db85224e4fec0667bbbb9b2a3e98ac82d49e885ddb4ce8c24f3e5a"
SERVER_URLS=(
  "http://ftp.lambda.moo.mud.org/pub/MOO/$SERVER_TGZ"
  "https://downloads.sourceforge.net/project/lambdamoo/lambdamoo/1.8.1/$SERVER_TGZ"
)
CORE_GZ="LambdaCore-17May04.db.gz"   # == LambdaCore-latest.db.gz on the same host
CORE_SHA256="f0eaa8f1154a511451fc469fc83df582a20700d772b780290f56f9273e69ea24"
CORE_URLS=(
  "http://ftp.lambda.moo.mud.org/pub/MOO/$CORE_GZ"
  "https://web.archive.org/web/20180719034434id_/http://ftp.lambda.moo.mud.org/pub/MOO/LambdaCore-latest.db.gz"
)

if [[ "${1:-}" == "--reset" ]]; then
  "$HERE/stop.sh" >/dev/null 2>&1 || true
  rm -rf "$RUN"
fi

mkdir -p "$OUT" "$RUN"

sha256_ok() {  # sha256_ok <file> <sha256>: GNU coreutils or the perl shasum
  local sum
  if command -v sha256sum >/dev/null; then sum="$(sha256sum "$1" | cut -d' ' -f1)"
  else sum="$(shasum -a 256 "$1" | cut -d' ' -f1)"; fi
  [[ "$sum" == "$2" ]]
}
patch_file() {  # patch_file <file> <sed expression>: in place, the same on BSD and GNU sed
  sed "$2" "$1" >"$1.tmp" && mv "$1.tmp" "$1"
}

fetch() {  # fetch <file> <sha256> <url>...
  local file="$1" sum="$2"; shift 2
  local dest="$BUILD/$file"
  if [[ -f "$dest" ]] && sha256_ok "$dest" "$sum"; then
    return 0
  fi
  local url
  for url in "$@"; do
    echo "fetching $url"
    if curl -fsSL -m 120 -o "$dest.part" "$url" && sha256_ok "$dest.part" "$sum"; then
      mv "$dest.part" "$dest"; return 0
    fi
    echo "  failed or checksum mismatch" >&2
  done
  rm -f "$dest.part"; echo "could not fetch $file" >&2; exit 1
}

fetch "$SERVER_TGZ" "$SERVER_SHA256" "${SERVER_URLS[@]}"
fetch "$CORE_GZ" "$CORE_SHA256" "${CORE_URLS[@]}"

# --- build ------------------------------------------------------------------
SRC="$OUT/MOO-1.8.1"
if [[ ! -x "$OUT/moo" ]]; then
  rm -rf "$SRC"
  tar -xzf "$BUILD/$SERVER_TGZ" -C "$OUT"
  # Modern compilers reject configure's ANSI probe (`char *argv` in main).
  patch_file "$SRC/configure" 's/int main(int argc, char \*argv)/int main(int argc, char **argv)/'
  # Listen on loopback only (1.8.1 has no bind-address option).
  patch_file "$SRC/net_bsd_tcp.c" \
    's/address.sin_addr.s_addr = htonl(INADDR_ANY);/address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);/'
  grep -q 'htonl(INADDR_LOOPBACK)' "$SRC/net_bsd_tcp.c"
  (
    cd "$SRC"
    unset CFLAGS CPPFLAGS LDFLAGS
    # K&R-era code: gnu89 keeps implicit declarations as warnings, -w hides them.
    export CC="cc -std=gnu89 -w"
    ./configure >configure.log 2>&1 || { tail -20 configure.log; exit 1; }
    make >make.log 2>&1 || { tail -40 make.log; exit 1; }
  )
  cp "$SRC/moo" "$OUT/moo"
  echo "built $OUT/moo"
fi

# --- working DB ---------------------------------------------------------------
if [[ -f "$RUN/moo.db" ]]; then
  echo "working DB exists: $RUN/moo.db (use --reset to rebuild it)"
  exit 0
fi

gunzip -c "$BUILD/$CORE_GZ" >"$RUN/pristine.db"
rm -f "$RUN/prep.db" "$RUN/prep.log"
"$OUT/moo" -l "$RUN/prep.log" "$RUN/pristine.db" "$RUN/prep.db" "$PORT" &
MOOPID=$!
trap 'kill $MOOPID 2>/dev/null || true' EXIT
python3 "$HERE/prepare_db.py" --port "$PORT"
# prepare_db.py ends with @shutdown; wait for the final dump.
wait $MOOPID || true
trap - EXIT
[[ -s "$RUN/prep.db" ]] || { echo "server did not write $RUN/prep.db" >&2; tail -20 "$RUN/prep.log"; exit 1; }
mv "$RUN/prep.db" "$RUN/moo.db"
echo "working DB ready: $RUN/moo.db"
