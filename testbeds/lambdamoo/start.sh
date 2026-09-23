#!/usr/bin/env bash
# Start the testbed LambdaMOO in the background on 127.0.0.1:${MOO_PORT:-17002}.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$HERE/.run"
PORT="${MOO_PORT:-17002}"
PIDFILE="$RUN/moo.pid"

[[ -x "$HERE/.build/moo" && -f "$RUN/moo.db" ]] || { echo "run ./setup.sh first" >&2; exit 1; }
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already running (pid $(cat "$PIDFILE"))"; exit 0
fi
# The server writes checkpoints/final dump to moo.db.new; promote the newest.
if [[ -s "$RUN/moo.db.new" ]]; then
  mv "$RUN/moo.db" "$RUN/moo.db.old"
  mv "$RUN/moo.db.new" "$RUN/moo.db"
fi
nohup "$HERE/.build/moo" -l "$RUN/moo.log" "$RUN/moo.db" "$RUN/moo.db.new" "$PORT" \
  >/dev/null 2>&1 </dev/null &
echo $! >"$PIDFILE"
for _ in $(seq 1 100); do   # wait up to ~20 s for the listener
  if grep -q "now listening on port $PORT" "$RUN/moo.log" 2>/dev/null && nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    echo "LambdaMOO up on 127.0.0.1:$PORT (pid $(cat "$PIDFILE"), log $RUN/moo.log)"; exit 0
  fi
  kill -0 "$(cat "$PIDFILE")" 2>/dev/null || { echo "server exited; see $RUN/moo.log" >&2; tail -5 "$RUN/moo.log" >&2; rm -f "$PIDFILE"; exit 1; }
  sleep 0.2
done
echo "server did not start listening; see $RUN/moo.log" >&2; exit 1
