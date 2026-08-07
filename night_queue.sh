#!/bin/bash
# Overnight series of H3 runs: light to heavy, up to the first out-of-memory failure.
#
#   nohup caffeinate -dimsu bash night_queue.sh > ~/models/logs/h3-night.log 2>&1 & disown
#
# Each run: per-phase timing (run_bench.py writes JSON) plus a memory profile
# (memwatch.sh writes CSV). A failing configuration is flagged and the series keeps
# going — a failure on the heaviest setting must not discard the lighter results.
#
# caffeinate, run outside this script, keeps the machine from sleeping for the
# duration of the series.

set -u
PROJ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$PROJ/.venv/bin/python"
LOGS="$HOME/models/logs"
SUMMARY="$LOGS/h3-night-summary.txt"

cd "$PROJ" || exit 1
mkdir -p "$LOGS"

# tag:width:height:duration — in increasing order of load.
# 1344x768 is H3's native resolution; everything else is off-distribution.
RUNS="
smoke:512:512:2.4
half:768:432:2.4
native5:1344:768:5.0
native10:1344:768:10.0
native15:1344:768:15.0
"

{
  echo "=== H3 overnight series ==="
  echo "start: $(date '+%Y-%m-%d %H:%M')"
  echo "machine: M4 Pro, 48 GB, GPU limit $(sysctl -n iogpu.wired_limit_mb 2>/dev/null) MB"
  echo
} | tee -a "$SUMMARY"

while IFS=: read -r tag w h dur; do
  [ -z "$tag" ] && continue

  echo "--- $tag: ${w}x${h}, ${dur}s -- start $(date '+%H:%M') ---" | tee -a "$SUMMARY"
  started=$(date +%s)

  "$PY" run_bench.py --width "$w" --height "$h" --duration "$dur" \
      --steps 31 --tag "$tag" > "$LOGS/h3-gen-$tag.log" 2>&1 &
  gen_pid=$!

  # wait for the process to hand its PID to python, then attach the monitor
  sleep 10
  real_pid=$(pgrep -f "run_bench.py --width $w --height $h" | head -1)
  [ -n "$real_pid" ] && bash memwatch.sh "$real_pid" "$LOGS/h3-mem-$tag.csv" 10 \
      > "$LOGS/h3-mem-$tag.err" 2>&1 &

  wait "$gen_pid"
  rc=$?
  elapsed=$(( $(date +%s) - started ))

  if [ "$rc" -eq 0 ]; then
    sps=$(grep -oE "[0-9.]+s per step" "$LOGS/h3-gen-$tag.log" | tail -1)
    peak=$(awk -F, 'NR>1 {if($2>r)r=$2} END {printf "%.1f", r}' "$LOGS/h3-mem-$tag.csv" 2>/dev/null)
    swap=$(awk -F, 'NR>1 {if($5>s)s=$5} END {printf "%.1f", s}' "$LOGS/h3-mem-$tag.csv" 2>/dev/null)
    echo "    DONE in $((elapsed / 60)) min | $sps | peak RSS ${peak} GB | swap ${swap} GB" | tee -a "$SUMMARY"
  else
    reason=$(grep -iE "error|out of memory|Traceback" "$LOGS/h3-gen-$tag.log" | tail -1 | cut -c1-120)
    echo "    FAILED (code $rc) after $((elapsed / 60)) min: $reason" | tee -a "$SUMMARY"
  fi
done <<< "$RUNS"

{
  echo
  echo "=== series finished: $(date '+%Y-%m-%d %H:%M') ==="
  ls -la "$HOME/models/video-out"/*.mp4 2>/dev/null | awk '{printf "  %6.1f MB  %s\n", $5/1048576, $9}'
} | tee -a "$SUMMARY"
