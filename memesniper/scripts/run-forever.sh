#!/usr/bin/env bash
# Keep memesniper running: restart it 10 seconds after any crash or disconnect.
# Usage (from the Ponytail folder, venv active):  bash memesniper/scripts/run-forever.sh [--live]
# Stop with Ctrl-C twice, or `python -m memesniper stop` to pause new entries while exits keep running.
cd "$(dirname "$0")/../.." || exit 1
mkdir -p logs
while true; do
  echo "$(date '+%F %T') starting memesniper $*" | tee -a logs/memesniper.log
  python -m memesniper run "$@" 2>&1 | tee -a logs/memesniper.log
  status=${PIPESTATUS[0]}
  if [ "$status" -eq 130 ]; then echo "stopped by Ctrl-C"; exit 0; fi
  echo "$(date '+%F %T') memesniper exited ($status); restarting in 10s" | tee -a logs/memesniper.log
  sleep 10
done
