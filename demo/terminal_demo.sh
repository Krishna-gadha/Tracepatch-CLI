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
# Story: a scientist has their own analysis script and their own dataset.
# The script crashes on the big file. TracePatch finds the line, fixes it,
# and proves the fix. Press Enter between steps so you can explain.
# Set NO_PAUSE=1 to run straight through.

cd "$(dirname "$0")/.." || exit 1
DATA="${1:-demo/big.csv}"
SCRIPT="${SCRIPT:-demo/safe_pipeline.py}"     # the scientist's script
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

step 1 "The scientist's dataset (on this laptop)"
say "This is the input file. It could be any CSV: here, a variant table."
run ls -lh "$DATA"
say "How many rows?"
echo "${bold}\$ wc -l $DATA${reset}"; wc -l < "$DATA" | awk '{printf "%\047d lines\n", $1}'
say "First 5 rows:"
echo "${bold}\$ head -6 $DATA${reset}"
if [[ "$DATA" == *.tsv ]]; then head -6 "$DATA" | cut -f1-8 | cut -c1-150; else head -6 "$DATA" | column -t -s,; fi
pause

step 2 "The scientist's analysis code"
LOAD_LINE=$(grep -n "read_csv" "$SCRIPT" | head -1 | cut -d: -f1)
say "A normal pandas script. Line $LOAD_LINE loads the WHOLE file into memory at once."
echo "${bold}\$ cat -n $SCRIPT${reset}"
awk -v from="$(grep -n '^import' "$SCRIPT" | head -1 | cut -d: -f1)" 'NR>=from { printf "%4d  %s\n", NR, $0 }' "$SCRIPT"
pause

step 3 "Run it the normal way: how much memory does it need?"
say "Measured by macOS itself (/usr/bin/time), not by TracePatch."
echo "${bold}\$ python3 $SCRIPT $DATA${reset}"
measure python3 "$SCRIPT" "$DATA"; MB=$PEAK
echo "${red}${bold}  → peak memory: ${MB} MB${reset}  (fine on this laptop, but it grows with the file:"
echo "    a 50 GB genomics file would need far more RAM than most machines have)"
pause

step 4 "Now pretend the machine only has ${CAP} MB of RAM"
say "The OS kills the script (exit 137): no error message, no line number."
say "TracePatch's Gate 1 runs it with a ${CAP} MB cap and records the line every 50 ms:"
run python3 -m tracepatch.gate1_tracer "$SCRIPT" "$DATA" --mem-limit-mb $CAP
pause

step 5 "Give the code + the dataset to TracePatch"
say "One command: trace → AST check → rewrite → sandbox test → full-scale check."
rm -rf tracepatch_out
run python3 -m tracepatch run "$SCRIPT" --data "$DATA" --mem-limit-mb $CAP --chunk-rows $CHUNK_ROWS
pause

if [ ! -f tracepatch_out/$NAME.patched.py ]; then
  echo "${red}No patch was made: this file fits under the ${CAP} MB cap, so there was nothing to fix.${reset}"
  echo "Use a bigger file, or a smaller cap, e.g.:  CAP=200 bash demo/terminal_demo.sh $DATA"
  exit 0
fi

step 6 "What did TracePatch change?"
run cat tracepatch_out/$NAME.diff
pause

step 7 "The scientist runs the FIXED script"
say "Same answer as before, but memory no longer grows with the file."
echo "${bold}\$ python3 tracepatch_out/$NAME.patched.py $DATA${reset}"
measure python3 tracepatch_out/$NAME.patched.py "$DATA"; MB2=$PEAK
echo "${green}${bold}  → peak memory: ${MB2} MB${reset}  (before: ${MB} MB)"
pause

step 8 "A script that must NOT be fixed: median"
say "The median of chunk medians is not the median. Chunking would give a WRONG answer silently."
FUNC_LINE=$(grep -n "^def " "$UNSAFE" | head -1 | cut -d: -f1)
run sed -n "${FUNC_LINE},$((FUNC_LINE + 2))p" "$UNSAFE"
run python3 -m tracepatch run "$UNSAFE" --data "$DATA" --mem-limit-mb $CAP
echo "${dim}# exit code: $? (2 = refused, nothing was changed)${reset}"

echo
echo "${bold}${green}Demo complete.${reset}"
