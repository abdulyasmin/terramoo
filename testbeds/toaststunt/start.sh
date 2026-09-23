#!/usr/bin/env bash
# Start the testbed MOO in the background on 127.0.0.1:${MOO_PORT:-17001}.
#   ./start.sh           keep the working DB (picks up the last shutdown dump)
#   ./start.sh --fresh   reset the working DB from base.db first
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
RUN="$HERE/.run"
PORT="${MOO_PORT:-17001}"
PID="$RUN/moo.pid"

[[ -x "$HERE/.build/moo" && -f "$RUN/base.db" ]] || { echo "run ./setup.sh first" >&2; exit 1; }

if [[ -f "$PID" ]] && kill -0 "$(cat "$PID")" 2>/dev/null; then
  echo "already running (pid $(cat "$PID"))"; exit 0
fi
rm -f "$PID"

if [[ "${1:-}" == "--fresh" ]]; then
  cp "$RUN/base.db" "$RUN/moo.db"; rm -f "$RUN/moo.db.new"
elif [[ -s "$RUN/moo.db.new" ]]; then
  mv "$RUN/moo.db.new" "$RUN/moo.db"      # adopt the last dump
fi
[[ -f "$RUN/moo.db" ]] || cp "$RUN/base.db" "$RUN/moo.db"

cd "$RUN"
nohup "$HERE/.build/moo" -l "$RUN/moo.log" -O -4 127.0.0.1 --no-ipv6 -p "$PORT" \
    moo.db moo.db.new < /dev/null > /dev/null 2>&1 &
echo $! > "$PID"

for _ in $(seq 1 100); do
  if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
    echo "listening on 127.0.0.1:$PORT (pid $(cat "$PID"), log $RUN/moo.log)"; exit 0
  fi
  kill -0 "$(cat "$PID")" 2>/dev/null || break
  sleep 0.1
done
echo "server failed to come up; see $RUN/moo.log" >&2
rm -f "$PID"; exit 1
