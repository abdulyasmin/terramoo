#!/usr/bin/env bash
# Stop the testbed MOO: SIGTERM makes the server dump moo.db.new and exit.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PID="$HERE/.run/moo.pid"

if [[ ! -f "$PID" ]]; then echo "not running"; exit 0; fi
pid="$(cat "$PID")"
if ! kill -0 "$pid" 2>/dev/null; then echo "not running (stale pidfile)"; rm -f "$PID"; exit 0; fi

kill -TERM "$pid"
for _ in $(seq 1 300); do
  kill -0 "$pid" 2>/dev/null || { rm -f "$PID"; echo "stopped"; exit 0; }
  sleep 0.1
done
echo "no clean exit after 30s; killing" >&2
kill -KILL "$pid" 2>/dev/null || true
rm -f "$PID"; exit 1
