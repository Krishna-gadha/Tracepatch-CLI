# TracePatch — Demo Runbook

What to type and what to say in the review, in order. The full explanations are
in [LEARNING_GUIDE.md](LEARNING_GUIDE.md).

## Before the review

Run these once, in the project folder, and check each result:

```bash
cd ~/Desktop/" Tracepatch"
```
```bash
ls -lh demo/data/clinvar.tsv
```
The file must exist (195M). If it is missing, use the download command in the
README (a few minutes).

```bash
git status --short demo/
```
Must print nothing. If `demo/clinvar_summary.py` shows as changed, a practice run
applied the repair; undo it with `python3 -m tracepatch restore demo/clinvar_summary.py`.

```bash
python3 -m pytest
```
Must end with `90 passed`.

```bash
ollama list
```
Must list `llama3.2` (if it says it cannot connect, open the Ollama app).

Then do **one full practice run** of the demo below, and `restore` afterwards. Also:
- make the terminal font large, and run `clear` before you start;
- close heavy apps, so memory readings are steady;
- keep this file open on the side.

## The demo (about 10 minutes with the LLM steps)

### 1. Set the scene (1 minute)

> "A genetics lab looks up each patient's DNA variants in ClinVar, the public
> database of what other labs found. The lab keeps a local copy of that table,
> and a bioinformatician has a small Python script that checks and summarises
> each new copy.
>
> The script does not run on a laptop. It is submitted as a job to a shared
> computing cluster, with a fixed memory limit. The table keeps growing. One day
> the script needs more memory than the job was given, and the cluster kills it.
> The log says only 'killed': no error, no line number.
>
> Today, people ask for more memory and submit again, or rewrite the script by
> hand and hope the answer is still right. TracePatch finds the line, repairs the
> script to read the table in pieces, and accepts the repair only after proving
> the answer is unchanged and the memory fits."

Say plainly that this is a typical scenario from how such clusters and tools are
documented to work, not a study of a particular lab.

### 2. The script and its data

```bash
cat demo/clinvar_summary.py
```

> "This is an analysis script. The data file is named at the top: ClinVar, NCBI's
> public database of human genetic variants. 300,000 records, 43 columns, 195 MB.
> Line 15 loads the whole table into memory, then it counts the records and the
> lab submissions."

```bash
python3 demo/clinvar_summary.py
```

> "On this laptop it runs, because the laptop has 24 GB. It needs 678 MB for a
> 195 MB file."

### 3. The problem

```bash
python3 -m tracepatch limit demo/clinvar_summary.py
```

> "On the cluster this would be `sbatch --mem=500M`. Our `limit` command plays the
> scheduler: with a 500 MB limit the script is killed. This is everything the
> person sees: no Python error, no line number. They only know it died."

### 4. TracePatch

```bash
python3 -m tracepatch
```

| It asks | You do | You say |
|---|---|---|
| Your analysis script | drag `demo/clinvar_summary.py` in (or type the path), Enter | "We give it only the script." |
| The dataset | nothing: it finds it | "It read the script and found which data file it uses." |
| Memory limit | just press Enter (500) | "The same 500 MB limit." |

As the lines appear:

| Line on screen | You say |
|---|---|
| `[Gate 1] KILLED ... fault at line 15` | "It ran the script and was killed again, but this time we recorded the line every 50 milliseconds, so we know it was line 15." |
| `[Gate 2] ... -> OK` | "We read the code and check every operation. `len`, `sum` and `max` can be computed piece by piece, so it is safe." |
| `[Rewrite] ... (25,000 rows at a time)` | "It rewrites the load to read 25,000 rows at a time. The chunk size is chosen from how wide the rows are." |
| `[Gate 3] ... taken from across the file: outputs match -> OK` | "The old and new code both run on 5,000 rows taken from across the file, in an isolated process. Same answer: both what they return and what they print." |
| `[Verify] peak ... MB -> OK` | "The repaired script runs on the whole file with the memory tracer attached. It stays far under the limit." |
| `VERIFIED PATCH` and the output | "Only now is the repair accepted. This is the real result: 300,000 records, 1.5 million lab submissions." |

- At `Show what was changed? [Y/n]` press **Enter**:
  > "Red is removed, green is added. One `read_csv` became a loop over chunks, and
  > the partial results are merged at the end."
- At `Replace clinvar_summary.py with the repaired version? [y/N]` type **y**, Enter:
  > "The original is kept as a backup."

### 5. The result

```bash
python3 -m tracepatch limit demo/clinvar_summary.py
```

> "The same script, under the same 500 MB limit that killed it. It finishes, with
> the same answer, at about 180 MB."

Then put the original back, so the next demo starts clean:

```bash
python3 -m tracepatch restore demo/clinvar_summary.py
```

### 6. A script that must be refused

```bash
python3 -m tracepatch run demo/clinvar_median.py
```

> "This computes a median. The median of the pieces is not the median of the whole
> file, so chunking would give a wrong answer with no error. Gate 2 refuses, and
> nothing is changed. Line 14 is where memory blew up; line 15 is why it can't be fixed."

### 7. The tests

```bash
python3 -m pytest
```

> "90 automated tests. They cover every gate, a patch with a deliberate bug that
> Gate 3 must catch, the retry loop stopping after three attempts, and every
> problem we found when we attacked our own tool. They also run automatically on
> Linux for every change."

### 8. Optional: a more meaningful question

```bash
python3 -m tracepatch run demo/clinvar_pathogenic.py
```

> "This counts how many records are pathogenic, meaning disease-causing: 115,014 of
> 300,000. Counting rows that match a condition is also safe to do in pieces."

## The LLM (free, on this laptop)

TracePatch uses a language model that runs **on the laptop** through Ollama:
`llama3.2`. No account, no key, no internet, no cost. It is used only when the
offline template cannot handle a function.

**Before the review**, check that Ollama is running (it is the llama icon in the
menu bar; if it is not there, open the Ollama app):

```bash
ollama list
```
It must list `llama3.2`.

### 9. An LLM-written repair that is verified

```bash
python3 -m tracepatch run demo/clinvar_needs_llm.py --rewriter auto
```

> "This script filters the pathogenic records and then sums their lab submissions.
> Our offline template does not know this pattern, so TracePatch asks a language
> model. Look at the Rewrite line: `local LLM (llama3.2)`. The model wrote the
> repair, but we do not trust it: Gate 3 compared it with the original on a sample,
> and the full-scale check measured its memory. Only then was it accepted."

To show the code the model wrote: `cat tracepatch_out/clinvar_needs_llm.diff`

### 10. An LLM-written repair that is rejected

```bash
python3 -m tracepatch run demo/clinvar_llm_rejected.py --rewriter auto
```

> "Here the model's rewrite is wrong: on the sample the original says 27 and the
> model's code says 5000. Gate 3 rejects it, the reason is sent back to the model,
> and it is asked again, three times at most. It never gets it right, so TracePatch
> fails loudly and changes nothing. This is the point of the project: the model can
> be wrong, and wrong code is never accepted."

### What to say about Claude

> "The same interface has a rewriter for Claude, a much stronger model. It is
> implemented and tested with a mock, but we have not run it live, because it needs
> a paid API key. The gates are the same whichever model writes the code."

## Numbers to remember

| What | Value |
|---|---|
| ClinVar file | 195 MB on disk, 300,000 records, 43 columns |
| Memory to load it all at once | 678 MB (measured by macOS) |
| Memory after the repair | about 180 MB |
| Memory limit in the demo | 500 MB |
| Rows per chunk (ClinVar) | 25,000 (about 16 MB of file per chunk) |
| Sample for Gate 3 | 5,000 rows from 5 places across the file, in 6 uneven chunks (997 rows) |
| Line sampling / cap check | every 50 ms / every 10 ms |
| Number tolerance | relative 1e-5, absolute 1e-8 |
| Retry cap | 3 attempts |
| Allowlist | 8 methods + 6 builtins |
| Tests | 90 |
| Awkward cases tried in the risk review | 25, none crashes |
| LLM | `llama3.2` (3 billion parameters, 2 GB), local through Ollama |
| Exit codes | 0 verified, 2 refused, 1 failed, 137 killed by `limit` |

## If something goes wrong

| Problem | What to do |
|---|---|
| Dragging a file doesn't paste the path | Type it: `demo/clinvar_summary.py` |
| "File not found" or "data file not found" | You must be in the project folder: `pwd` should end in ` Tracepatch` |
| `NO_PROBLEM: ... stayed under the cap` | The script is already repaired. Run `python3 -m tracepatch restore demo/clinvar_summary.py` |
| `limit` finishes instead of being killed | Same cause: `restore` the script |
| The Rewrite line says `template rewriter could not rewrite` in steps 9–10 | Ollama is not running: open the Ollama app, check `ollama list`, and run again |
| Interactive mode misbehaves | Use one command: `python3 -m tracepatch run demo/clinvar_summary.py` |
| `clinvar.tsv` is missing or broken | Use the synthetic data: `python3 demo/make_data.py`, then `python3 -m tracepatch run demo/safe_pipeline.py --data demo/big.csv` and the same with `demo/unsafe_pipeline.py` |
| A run fails unexpectedly | Run it once more (memory readings vary a little), then use the synthetic data |

## The most likely questions

1. **What is new here?** Not chunking, but the checks around it: a rewrite is accepted only after the safety check, the sample comparison and the full-scale memory check all pass.
2. **Is this a real problem?** Yes. Clusters kill jobs that exceed their memory limit (Slurm calls it an "oom-kill event"), and Nextflow's documented recipe is to retry with more memory on exit status 137.
3. **How do people solve it today?** Mostly by asking for more memory and re-running; or rewriting by hand; or switching to Dask, Polars or DuckDB. None of these checks that the answer stayed the same.
4. **Would scientists use this as it is?** No. It is a prototype that repairs a small set of patterns. It demonstrates the method: verify an automatic repair before trusting it.
5. **How do you find the line with no traceback?** A thread writes the current line and memory to a file every 50 ms and flushes it, so the record survives the kill.
6. **Why refuse the median?** The median of chunk medians is not the median: `[1,2,3]` and `[4,100]` give 27 instead of 3.
7. **Why an allowlist?** A blocklist allows anything it forgot; an allowlist refuses anything it doesn't know. Wrong "safe" answers are silent, so we fail safe.
8. **Why a tolerance for numbers?** Chunking changes the order of additions, and `(0.1+0.2)+0.3` is not exactly `0.1+(0.2+0.3)`.
9. **Does passing the sample prove it works on the full file?** No. That is why the full-scale check exists. On our first ClinVar run it caught a patch that still used over 600 MB. Now, when a chunk is too big, it halves the chunk size and checks again.
10. **Your laptop has 24 GB. Is the crash real?** The limit is simulated by our watchdog, the way a cluster scheduler enforces a job's limit. Without it this script needs 678 MB; bigger files need far more.
11. **Where is the AI?** A language model writes the repairs the template can't (we run `llama3.2` locally), and the project's point is making LLM-written code trustworthy by verifying it. The gates themselves are program analysis and testing.
12. **What are the limits?** The LLM is a small local model; checking on a sample is not a proof; only operations known to be chunk-safe are repaired; the loading must be inside a function; CSV/TSV with pandas only. Each one ends in a clear message, never a wrong answer.
13. **What could go wrong, and did you test that?** Yes. We attacked our own tool with 25 awkward scripts and data files. It found two crashes and one false "verified", which we fixed. All 25 now end cleanly. The list is in `docs/RISK_REVIEW.md`.

More questions with answers: LEARNING_GUIDE.md, section 18. Real-world context
and sources: section 21. Everything that could go wrong: RISK_REVIEW.md.

## Say these plainly if asked

- It has limits, and we list them. What we claim is that it never accepts a repair it could not verify, and never fails silently.
- We did not survey scientists. What we know about real use comes from public documentation (Slurm, Nextflow, pandas), listed in section 21 of the guide.
- The demo scripts are simple on purpose. The project is the repair and its verification, not the analysis.
- It makes no biological claims. ClinVar is used as a real, large table.
- The full-scale check proves the repaired script fits in memory. Correctness comes from the sample comparison.
- If you are asked which tools you used to build it, including AI coding assistants, answer honestly. What matters in a viva is that you can explain every file, and the learning guide is there for that.
