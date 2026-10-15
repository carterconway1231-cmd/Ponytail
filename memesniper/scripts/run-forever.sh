#!/usr/bin/env bash
# Keep memesniper running: restart it 10 seconds after any crash or disconnect.
# Usage (from the Ponytail folder, venv active):  bash memesniper/scripts/run-forever.sh [--live]
# Stop with Ctrl-C (or a TERM signal from a process manager): the bot is stopped cleanly, no restart.
# To pause new buys but keep managing open positions: python -m memesniper stop
cd "$(dirname "$0")/../.." || exit 1
mkdir -p logs
child=""
stop() {
  echo "$(date '+%F %T') stopping memesniper" | tee -a logs/memesniper.log
  [ -n "$child" ] && kill -INT "$child" 2>/dev/null && wait "$child" 2>/dev/null
  exit 0
}
trap stop INT TERM
while true; do
  echo "$(date '+%F %T') starting memesniper $*" | tee -a logs/memesniper.log
  python -m memesniper run "$@" > >(tee -a logs/memesniper.log) 2>&1 &
  child=$!
  wait "$child"
  status=$?
  child=""
  echo "$(date '+%F %T') memesniper exited ($status); restarting in 10s" | tee -a logs/memesniper.log
  sleep 10 &
  wait $!
done
