#!/usr/bin/env bash
# Stop the testbed LambdaMOO.  SIGTERM makes the server dump to moo.db.new first;
# start.sh promotes that dump.  `stop.sh --discard` drops the dump instead.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$HERE/.run"
PIDFILE="$RUN/moo.pid"

if [[ ! -f "$PIDFILE" ]]; then echo "not running"; exit 0; fi
PID="$(cat "$PIDFILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill -TERM "$PID"
  for _ in $(seq 1 150); do kill -0 "$PID" 2>/dev/null || break; sleep 0.2; done
  if kill -0 "$PID" 2>/dev/null; then echo "still running after 30 s; sending KILL" >&2; kill -KILL "$PID"; fi
fi
rm -f "$PIDFILE"
[[ "${1:-}" == "--discard" ]] && rm -f "$RUN/moo.db.new"
echo "stopped"
