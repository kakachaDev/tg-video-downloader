#!/bin/bash
set -e
cd "$(dirname "$0")"

# Kill existing wrapper loop (prevents auto-restart)
if [ -f wrapper.pid ] && kill -0 "$(cat wrapper.pid)" 2>/dev/null; then
    echo "Stopping wrapper loop (PID $(cat wrapper.pid))..."
    kill "$(cat wrapper.pid)"
    sleep 1
fi

# Kill existing Python bot process
if [ -f bot.pid ] && kill -0 "$(cat bot.pid)" 2>/dev/null; then
    echo "Stopping bot process (PID $(cat bot.pid))..."
    kill "$(cat bot.pid)"
    sleep 1
fi

rm -f wrapper.pid bot.pid

(
    while true; do
        .venv/bin/python bot.py >> bot.log 2>&1
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Bot exited, restarting in 10s..." >> bot.log
        sleep 10
    done
) &

echo $! > wrapper.pid
echo "Bot started (wrapper PID $(cat wrapper.pid))"
