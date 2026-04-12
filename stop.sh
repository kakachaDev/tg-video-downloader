#!/bin/bash
if [ -f bot.pid ] && kill -0 "$(cat bot.pid)" 2>/dev/null; then
    echo "Stopping bot (PID $(cat bot.pid))..."
    kill "$(cat bot.pid)"
    rm -f bot.pid
    echo "Done."
else
    echo "Bot is not running."
    rm -f bot.pid
fi
