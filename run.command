#!/bin/bash
cd "$(dirname "$0")" || exit 1
export PORT="${PORT:-8080}"
export HOST="${HOST:-0.0.0.0}"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv || exit 1
fi
.venv/bin/python -m pip install -q -r requirements.txt || exit 1
EXISTING_PID="$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null)"
if [ -n "$EXISTING_PID" ]; then
  echo "Port $PORT is already in use (PID $EXISTING_PID) - stopping it..."
  kill $EXISTING_PID 2>/dev/null
  sleep 1
  kill -9 $EXISTING_PID 2>/dev/null
  sleep 1
fi
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)"
echo "-------------------------------------------------------"
echo " Postage Reporting is starting on port $PORT"
echo "   This computer:  http://127.0.0.1:$PORT"
[ -n "$LAN_IP" ] && echo "   Other computers: http://$LAN_IP:$PORT"
echo " (macOS may prompt to allow incoming connections - click Allow)"
echo "-------------------------------------------------------"
( sleep 2; open "http://127.0.0.1:$PORT" ) &
exec .venv/bin/python app.py
