#!/usr/bin/env bash
# Start mooR in the background: moor-daemon (database + VM), moor-telnet-host
# (127.0.0.1:17003) and moor-web-host (HTTP API, 127.0.0.1:17080). They talk
# over ZeroMQ IPC sockets in .run/ipc (IPC needs no CURVE keys/enrollment).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
RUN=$HERE/.run
BIN=$HERE/.build/moor/target/release-fast
CORE=$HERE/.build/moor/cores/lambda-moor/src
IPC=ipc://$RUN/ipc

for b in moor-daemon moor-telnet-host moor-web-host; do
  [ -x "$BIN/$b" ] || { echo "no $BIN/$b; run ./setup.sh first" >&2; exit 1; }
done
alive() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }
if alive daemon; then echo "already running (daemon pid $(cat "$RUN/daemon.pid"))"; exit 0; fi
mkdir -p "$RUN/config" "$RUN/share" "$RUN/data" "$RUN/ipc" "$RUN/telnet-host" "$RUN/web-host" "$RUN/logs"
# XDG dirs keep the PASETO keypair (daemon) and host state inside .run/.
export XDG_CONFIG_HOME=$RUN/config XDG_DATA_HOME=$RUN/share RUST_BACKTRACE=1

launch() {  # name, command...
  local name=$1; shift
  nohup "$@" >>"$RUN/logs/$name.log" 2>&1 </dev/null &
  echo $! >"$RUN/$name.pid"
}
port_open() { nc -z 127.0.0.1 "$1" >/dev/null 2>&1; }
wait_for() {  # what, test-command...
  local what=$1; shift
  for _ in $(seq 1 240); do
    "$@" && return 0
    for p in daemon telnet-host web-host; do
      if [ -f "$RUN/$p.pid" ] && ! alive $p; then
        echo "$p exited during startup; see $RUN/logs/$p.log" >&2; "$HERE/stop.sh"; exit 1
      fi
    done
    sleep 0.5
  done
  echo "timed out waiting for $what" >&2; "$HERE/stop.sh"; exit 1
}

# --import only takes effect while .run/data/world.db does not exist yet.
launch daemon "$BIN/moor-daemon" "$RUN/data" \
  --db world.db --config-file "$HERE/moor.yaml" \
  --import "$CORE" --import-format objdef --generate-keypair \
  --rpc-listen "$IPC/rpc.sock" --events-listen "$IPC/events.sock" \
  --workers-request-listen "$IPC/workers-request.sock" \
  --workers-response-listen "$IPC/workers-response.sock"
wait_for "daemon rpc socket" test -S "$RUN/ipc/rpc.sock"

launch telnet-host "$BIN/moor-telnet-host" \
  --telnet-address 127.0.0.1 --telnet-port 17003 --health-check-port 17988 \
  --rpc-address "$IPC/rpc.sock" --events-address "$IPC/events.sock" \
  --data-dir "$RUN/telnet-host"
launch web-host "$BIN/moor-web-host" \
  --listen-address 127.0.0.1:17080 \
  --rpc-address "$IPC/rpc.sock" --events-address "$IPC/events.sock" \
  --data-dir "$RUN/web-host"

wait_for "telnet 127.0.0.1:17003" port_open 17003
wait_for "http 127.0.0.1:17080" port_open 17080
echo "mooR up: telnet 127.0.0.1:17003, http 127.0.0.1:17080 (pids in $RUN/*.pid)"
