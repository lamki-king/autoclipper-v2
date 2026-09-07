#!/bin/sh
set -eu

BGUTIL_DIR=/opt/bgutil-ytdlp-pot-provider/server
BGUTIL_LOG=/tmp/bgutil.log

if [ -f "$BGUTIL_DIR/build/main.js" ]; then
  cd "$BGUTIL_DIR"
  node build/main.js --port 4416 >"$BGUTIL_LOG" 2>&1 &
  BGUTIL_PID=$!
else
  echo "bgutil provider build not found" >&2
  BGUTIL_PID=""
fi

# Give bgutil a short chance to start, but never prevent the public API
# from starting: the downloader has its own client fallbacks.
if [ -n "$BGUTIL_PID" ]; then
  for i in $(seq 1 10); do
    if curl -fsS http://127.0.0.1:4416/ >/dev/null 2>&1; then
      break
    fi
    if ! kill -0 "$BGUTIL_PID" 2>/dev/null; then
      cat "$BGUTIL_LOG" >&2 || true
      BGUTIL_PID=""
      break
    fi
    sleep 1
done
fi

if [ -n "$BGUTIL_PID" ]; then
  trap 'kill "$BGUTIL_PID" 2>/dev/null || true' EXIT
fi

exec uvicorn app.entry:app --host 0.0.0.0 --port "${PORT:-10000}"
