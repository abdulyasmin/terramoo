#!/usr/bin/env bash
# Stop what start.sh started: hosts first, then the daemon (SIGTERM; SIGKILL after 30 s).
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
RUN=$HERE/.run
stopped=0
for name in web-host telnet-host daemon; do
  f=$RUN/$name.pid
  [ -f "$f" ] || continue
  pid=$(cat "$f")
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid"
    for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && { echo "$name still alive; SIGKILL"; kill -KILL "$pid"; }
    stopped=1
  fi
  rm -f "$f"
done
[ $stopped = 1 ] && echo "stopped" || echo "not running"
