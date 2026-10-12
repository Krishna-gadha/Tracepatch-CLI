#!/bin/bash
# TracePatch terminal walkthrough for a live demo.
#
#   bash demo/terminal_demo.sh                    # uses demo/big.csv (generated if missing)
#   bash demo/terminal_demo.sh path/to/your.csv   # "upload" your own CSV (needs a depth column)
#   CAP=200 bash demo/terminal_demo.sh file.csv   # smaller files need a smaller memory cap
#
# Real ClinVar data (see demo/clinvar_summary.py for the download command):
#   SCRIPT=demo/clinvar_summary.py UNSAFE=demo/clinvar_median.py CAP=500 CHUNK_ROWS=25000 \
#     bash demo/terminal_demo.sh demo/data/clinvar.tsv
#
# Process: load a dataset, run an analysis script on it, watch it crash under
# a memory limit, let TracePatch find the line, repair it and prove the repair.
# Press Enter between steps so you can explain.
# Set NO_PAUSE=1 to run straight through.

cd "$(dirname "$0")/.." || exit 1
DATA="${1:-demo/big.csv}"
SCRIPT="${SCRIPT:-demo/safe_pipeline.py}"     # the analysis script
UNSAFE="${UNSAFE:-demo/unsafe_pipeline.py}"   # the median script (must be refused)
CHUNK_ROWS="${CHUNK_ROWS:-100000}"            # wide tables (e.g. ClinVar) need fewer
NAME=$(basename "$SCRIPT" .py)
CAP="${CAP:-300}"   # override: CAP=200 bash demo/terminal_demo.sh file.csv

bold=$(tput bold 2>/dev/null); dim=$(tput dim 2>/dev/null); reset=$(tput sgr0 2>/dev/null)
blue=$(tput setaf 4 2>/dev/null); green=$(tput setaf 2 2>/dev/null); red=$(tput setaf 1 2>/dev/null)

step() {
  echo
  echo "${bold}${blue}════════════════════════════════════════════════════════════════${reset}"
  echo "${bold}${blue}  STEP $1: $2${reset}"
  echo "${bold}${blue}════════════════════════════════════════════════════════════════${reset}"
}
say()   { echo "${dim}# $*${reset}"; }
run()   { echo "${bold}\$ $*${reset}"; "$@"; }
pause() { [ -n "$NO_PAUSE" ] || read -r -p "${dim}   ⏎ press Enter to continue${reset}"; }

# Run a command, print its output, and set PEAK to its peak memory in MB,
# as measured by the operating system itself (macOS /usr/bin/time -l).
measure() {
  /usr/bin/time -l "$@" > /tmp/tracepatch_out.txt 2> /tmp/tracepatch_time.txt
  sed 's/^/  /' /tmp/tracepatch_out.txt
  PEAK=$(awk '/maximum resident set size/ {printf "%.0f", $1/1048576}' /tmp/tracepatch_time.txt)
}

if [ ! -f "$DATA" ]; then
  if [ "$DATA" = "demo/big.csv" ]; then
    say "demo/big.csv not found, generating it (4 million rows)..."
    python3 demo/make_data.py
  else
    echo "${red}File not found: $DATA${reset}"; exit 1
  fi
fi

step 1 "The dataset (a file on disk, not loaded yet)"
say "This file is saved on the laptop's disk. Nothing has been loaded into memory yet."
run ls -lh "$DATA"
say "How many rows?"
echo "${bold}\$ wc -l $DATA${reset}"; wc -l < "$DATA" | awk '{printf "%\047d lines\n", $1}'
if [[ "$DATA" == *.tsv ]]; then
  say "It has $(head -1 "$DATA" | awk -F'\t' '{print NF}') columns. Four of them:"
  echo "${bold}\$ head -6 $DATA | cut -f2,5,7,26${reset}"
  head -6 "$DATA" | cut -f2,5,7,26 | column -t -s $'\t'
else
  say "First 5 rows:"
  echo "${bold}\$ head -6 $DATA${reset}"; head -6 "$DATA" | column -t -s,
fi
pause

step 2 "The analysis script"
LOAD_LINE=$(grep -n "read_csv" "$SCRIPT" | head -1 | cut -d: -f1)
say "Line $LOAD_LINE is where the file gets loaded: read_csv copies the WHOLE file from disk into memory (RAM)."
echo "${bold}\$ cat -n $SCRIPT${reset}"
awk -v from="$(grep -n '^import' "$SCRIPT" | head -1 | cut -d: -f1)" 'NR>=from { printf "%4d  %s\n", NR, $0 }' "$SCRIPT"
pause

step 3 "Run the script normally (this is when the file is loaded)"
say "Measured by macOS itself (/usr/bin/time), not by TracePatch."
echo "${bold}\$ python3 $SCRIPT $DATA${reset}"
measure python3 "$SCRIPT" "$DATA"; MB=$PEAK
echo "${red}${bold}  → peak memory: ${MB} MB${reset}  (the file is $(du -h "$DATA" | cut -f1)B on disk; loaded into RAM it needs much more)"
pause

step 4 "Run it with a ${CAP} MB memory limit: it crashes"
say "Like a cluster job or a smaller machine. The OS kills it: exit 137, no error, no line number."
say "TracePatch Gate 1 records which line was running when it was killed:"
run python3 -m tracepatch.gate1_tracer "$SCRIPT" "$DATA" --mem-limit-mb $CAP
pause

step 5 "Repair it with TracePatch"
say "One command: trace → AST check → rewrite → sandbox test → full-scale check."
rm -rf tracepatch_out
run python3 -m tracepatch run "$SCRIPT" --data "$DATA" --mem-limit-mb $CAP --chunk-rows $CHUNK_ROWS
pause

if [ ! -f tracepatch_out/$NAME.patched.py ]; then
  echo "${red}No patch was made: this file fits under the ${CAP} MB cap, so there was nothing to fix.${reset}"
  echo "Use a bigger file, or a smaller cap, e.g.:  CAP=200 bash demo/terminal_demo.sh $DATA"
  exit 0
fi

step 6 "What was changed"
run cat tracepatch_out/$NAME.diff
pause

step 7 "Run the repaired script"
say "Same answer as step 3, but memory no longer grows with the file."
echo "${bold}\$ python3 tracepatch_out/$NAME.patched.py $DATA${reset}"
measure python3 tracepatch_out/$NAME.patched.py "$DATA"; MB2=$PEAK
echo "${green}${bold}  → peak memory: ${MB2} MB${reset}  (before: ${MB} MB)"
pause

step 8 "An unsafe script is refused (median)"
say "The median of chunk medians is not the median. Chunking would give a WRONG answer silently."
FUNC_LINE=$(grep -n "^def " "$UNSAFE" | head -1 | cut -d: -f1)
run sed -n "${FUNC_LINE},$((FUNC_LINE + 2))p" "$UNSAFE"
run python3 -m tracepatch run "$UNSAFE" --data "$DATA" --mem-limit-mb $CAP
echo "${dim}# exit code: $? (2 = refused, nothing was changed)${reset}"

echo
echo "${bold}${green}Demo complete.${reset}"
