# TracePatch — Demo Runbook

What to type and what to say in the review, in order. The full explanations are
in [LEARNING_GUIDE.md](LEARNING_GUIDE.md).

## Before the review

Run these once, in the project folder, and check each result:

```bash
cd ~/Desktop/" Tracepatch"
```
```bash
ls -lh demo/data/clinvar.tsv demo/big.csv
```
Both files must exist (195M and 111M). If `big.csv` is missing: `python3 demo/make_data.py`.
If `clinvar.tsv` is missing, use the download command in the README (a few minutes).

```bash
python3 -m pytest
```
Must end with `54 passed`.

Then do **one full practice run** of the demo below. Also:
- make the terminal font large, and run `clear` before you start;
- close heavy apps, so memory readings are steady;
- keep this file open on the side.

## The demo (about 6 minutes)

### 1. Say what it is (20 seconds)

> "Data scripts that load a huge file at once get killed by the operating system,
> with no error message and no line number. TracePatch finds the line, repairs the
> script to read the file in pieces, and accepts the repair only after verifying it."

### 2. Repair a script on real data

```bash
python3 -m tracepatch
```

| It asks | You do | You say |
|---|---|---|
| Upload your dataset | drag `demo/data/clinvar.tsv` in (or type the path), Enter | "This is ClinVar, NCBI's public database of human genetic variants: 300,000 records, 43 columns, 195 MB." |
| Upload your analysis script | drag `demo/clinvar_summary.py` in, Enter | "This is the analysis script. Line 14 loads the whole file into memory." |
| Memory limit | just press Enter (500) | "We allow 500 MB, like the memory limit of a cluster job." |

As the lines appear:

| Line on screen | You say |
|---|---|
| `[Gate 1] KILLED ... fault at line 14` | "The script went over the limit and was killed. Normally that gives no line number. We record the line every 50 milliseconds, so we know it was line 14." |
| `[Gate 2] ... -> OK` | "We read the code and check every operation. `len`, `sum` and `max` can be computed piece by piece, so it is safe." |
| `[Rewrite] ...` | "It rewrites the load to read 25,000 rows at a time." |
| `[Gate 3] ... outputs match -> OK` | "The old and new code both run on a 5,000-row sample in an isolated process. Same answer." |
| `[Verify] peak ... MB -> OK` | "The repaired script runs on the whole file with the memory tracer attached. It stays far under the limit." |
| `VERIFIED PATCH` and the output | "Only now is the repair accepted. And this is the real result: 300,000 records, 1.5 million lab submissions." |

At `Show what was changed? [Y/n]` press Enter:

> "Red is removed, green is added. One `read_csv` became a loop over chunks, and the
> partial results are merged at the end."

### 3. A more meaningful question (optional, 1 minute)

Run `python3 -m tracepatch` again with `demo/clinvar_pathogenic.py`.

> "This counts how many records are pathogenic, meaning disease-causing: 115,014 of
> 300,000. Counting rows that match a condition is also safe to do in pieces."

### 4. A script that must be refused

Run `python3 -m tracepatch` again with `demo/clinvar_median.py`.

> "This computes a median. The median of the pieces is not the median of the whole
> file, so chunking would give a wrong answer with no error. Gate 2 refuses, and
> nothing is changed. Line 12 is where memory blew up; line 13 is why it can't be fixed."

### 5. The tests

```bash
python3 -m pytest
```

> "54 automated tests cover every gate, including a patch with a deliberate bug that
> Gate 3 must catch, and a check that the retry loop stops after three attempts."

### 6. If they ask about the LLM

```bash
python3 -m tracepatch run demo/clinvar_needs_llm.py --data demo/data/clinvar.tsv --mem-limit-mb 500 --chunk-rows 25000
```

> "This script filters rows and then sums. Gate 2 says it is safe, but our offline
> template doesn't know this pattern, so it stops and says so. This is where the LLM
> rewriter plugs in: Claude writes the repair and the same gates verify it. That part
> is implemented and tested with a mock, but we have not run it live, because it
> needs a paid API key."

## Numbers to remember

| What | Value |
|---|---|
| ClinVar file | 195 MB on disk, 300,000 records, 43 columns |
| Memory to load it all at once | 678 MB (measured by macOS) |
| Memory after the repair | about 180 MB |
| Memory limit in the demo | 500 MB |
| Rows per chunk (ClinVar) | 25,000 |
| Sample for Gate 3 | first 5,000 rows, split into 5 chunks |
| Line sampling / cap check | every 50 ms / every 10 ms |
| Number tolerance | relative 1e-5, absolute 1e-8 |
| Retry cap | 3 attempts |
| Allowlist | 8 methods + 6 builtins |
| Tests | 54 |
| Exit codes | 0 verified, 2 refused, 1 failed |

## If something goes wrong

| Problem | What to do |
|---|---|
| Dragging a file doesn't paste the path | Type it: `demo/data/clinvar.tsv`, `demo/clinvar_summary.py` |
| "File not found" | Check you are in the project folder: `pwd` should end in ` Tracepatch` |
| `NO_PROBLEM: ... stayed under the cap` | The limit was too high for that file. Use 500 for ClinVar, 300 for `big.csv` |
| Interactive mode misbehaves | Use one command: `python3 -m tracepatch run demo/clinvar_summary.py --data demo/data/clinvar.tsv --mem-limit-mb 500 --chunk-rows 25000` |
| `clinvar.tsv` is missing or broken | Use the synthetic data: `python3 demo/make_data.py`, then `python3 -m tracepatch run demo/safe_pipeline.py --data demo/big.csv --mem-limit-mb 300` and the same with `demo/unsafe_pipeline.py` |
| A run fails unexpectedly | Run it once more (memory readings vary a little), then use the synthetic data |

## The ten most likely questions

1. **What is new here?** Not chunking, but the checks around it: a rewrite is accepted only after the safety check, the sample comparison and the full-scale memory check all pass.
2. **How do you find the line with no traceback?** A thread writes the current line and memory to a file every 50 ms and flushes it, so the record survives the kill.
3. **Why refuse the median?** The median of chunk medians is not the median: `[1,2,3]` and `[4,100]` give 27 instead of 3.
4. **Why an allowlist?** A blocklist allows anything it forgot; an allowlist refuses anything it doesn't know. Wrong "safe" answers are silent, so we fail safe.
5. **Why a tolerance for numbers?** Chunking changes the order of additions, and `(0.1+0.2)+0.3` is not exactly `0.1+(0.2+0.3)`.
6. **Does passing the sample prove it works on the full file?** No. That is why the full-scale check exists. On our first ClinVar run it caught a patch that still used over 600 MB.
7. **Your laptop has 24 GB. Why does it crash?** The cap simulates a cluster job's memory limit. Without the cap this script needs 678 MB; bigger files need far more.
8. **Where is the AI?** The LLM rewriter, and making LLM-written code trustworthy by verifying it. The gates themselves are program analysis and testing.
9. **Have you run the LLM?** Not live. It is implemented and tested with a mock; the verified demo uses the offline template.
10. **What are the limits?** Small allowlist, simple template patterns, sample-based checking, data path must be an argument, CSV/TSV only, macOS only, process-level sandbox.

More questions with answers: LEARNING_GUIDE.md, section 18.

## Say these plainly if asked

- The demo scripts are simple on purpose. The project is the repair and its verification, not the analysis.
- It makes no biological claims. ClinVar is used as a real, large table.
- The full-scale check proves the repaired script fits in memory. Correctness comes from the sample comparison.
- If you are asked which tools you used to build it, including AI coding assistants, answer honestly. What matters in a viva is that you can explain every file, and the learning guide is there for that.
