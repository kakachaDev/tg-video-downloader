#!/bin/bash
cd "$(dirname "$0")"
STOPPED=0

# Kill wrapper loop first (prevents auto-restart)
if [ -f wrapper.pid ] && kill -0 "$(cat wrapper.pid)" 2>/dev/null; then
    echo "Stopping wrapper loop (PID $(cat wrapper.pid))..."
    kill "$(cat wrapper.pid)"
    rm -f wrapper.pid
    STOPPED=1
else
    rm -f wrapper.pid
fi

# Kill Python bot process
if [ -f bot.pid ] && kill -0 "$(cat bot.pid)" 2>/dev/null; then
    echo "Stopping bot process (PID $(cat bot.pid))..."
    kill "$(cat bot.pid)"
    rm -f bot.pid
    STOPPED=1
else
    rm -f bot.pid
fi

if [ $STOPPED -eq 1 ]; then
    echo "Done."
else
    echo "Bot is not running."
fi
