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
Must end with `98 passed`.

```bash
ollama list
```
Must list `llama3.2` (if it says it cannot connect, open the Ollama app).

Then do **one full practice run** of the demo below, and `restore` afterwards. Also:
- make the terminal font large, and run `clear` before you start;
- close heavy apps, so memory readings are steady;
- keep this file open on the side.

## The demo (about 10 minutes with the LLM steps)

### Before you touch the terminal: explain it in plain words (2 minutes)

Do not start with the terminal. People who do not write code will be lost in
ten seconds. Say this first, slowly. No technical words until the end.

**1. A picture everyone understands**

> "Imagine counting the money in a very large bag, on a small table.
>
> If you pour the whole bag onto the table, it does not fit. Notes fall on the
> floor and you have to start again.
>
> The sensible way is to take **one handful at a time**, count it, write down the
> total so far, put it aside, and take the next handful. The table never
> overflows, and the final total is the same."

**2. Connect it to computers**

> "A computer has the same problem. The bag is a **large data file**. The table
> is the computer's **memory**, its working space. Many programs pour the whole
> file into memory at once. When it does not fit, the computer simply **shuts the
> program down**. It does not say why, and it does not say where."

**3. Who this happens to**

> "This happens to **scientists who analyse large data**. Our example is a
> hospital genetics lab. They read a patient's DNA and look up what is known
> about it in a large public table called ClinVar. They write small programs to
> analyse that table. The table grows every week. One day the program that
> worked last month is shut down, and they do not know why."

**4. What they do today**

> "Today they have three choices. Ask for a **bigger computer** and try again,
> which costs time and money and fails again when the table grows. **Rewrite the
> program by hand**, which is slow. Or **ask an AI chatbot** to rewrite it, and
> hope the new program still gives the right answer. Nothing checks that."

**5. The catch**

> "And here is the danger. 'One handful at a time' works for a **total**. It does
> **not** work for everything. If I ask for the **middle value** of all the notes,
> I cannot find it handful by handful. A program rewritten that way gives a
> **wrong answer, with no warning**. That is worse than a program that stops."

**6. What we built**

> "TracePatch does four things with one command. It **finds the exact line**
> where the program ran out of memory. It **checks whether the calculation is
> safe** to do one handful at a time. It **rewrites the program**. And it
> **proves** the new program gives the same answer and fits in memory. If the
> calculation is not safe, like the middle value, it **refuses and says what to
> do instead**. It never gives a wrong answer to look helpful."

**7. The benefit, in one line**

> "The scientist gets a program that works on the same computer, with the same
> answer, in seconds, instead of days of trial and error."

Then say: *"Let me show you."* Only now open the terminal.

**While the demo runs, point at only three things.** Ignore the rest of the text.

| Point at | Say |
|---|---|
| `KILLED ... fault at line 15` | "The program was shut down. We found the line." |
| `VERIFIED PATCH` | "It was repaired, and the repair was checked." |
| `memory before ... / memory after ...` | "It needed more than 500. Now it needs 180. Same answer." |

**If they ask "is this used in the real world?"**

> "Not yet. This is a working prototype. The problem is real: the standard tools
> scientists use on shared computers have a built-in 'try again with more
> memory' feature, because this happens so often. We show a way to fix the
> program instead, and to check the fix."

**If they ask "does it only work on this one dataset?"** Show a different one:

```bash
python3 -m tracepatch run demo/safe_pipeline.py --data demo/big.csv
```

> "A different file and a different program: 4 million rows, different columns.
> It works on any table file, as long as the calculation is one it knows is safe."

**Who writes the repair?** For the main demo, TracePatch's own built-in rules,
with no AI. The screen says `template rewriter`. Only in steps 9 and 10 does the
AI model write it, and the screen says `local LLM (llama3.2)`.

### The short version: one command

If there is little time, this single command shows everything: it finds the
crash, repairs the script, verifies the repair, replaces the script and prints
what it changed.

```bash
python3 -m tracepatch demo/clinvar_summary.py
```

Afterwards: `python3 -m tracepatch restore demo/clinvar_summary.py`

The steps below show the same thing more slowly, with the before and after.

### 1. Set the scene (only if you skipped the plain-words opening)

> "A genetics lab looks up each patient's DNA variants in ClinVar, the public
> database of what other labs found. A bioinformatician's script checks each new
> copy of that table. The script runs as a job on a shared cluster with a fixed
> memory limit. The table keeps growing; one day the cluster kills the job, with
> no error and no line number. TracePatch finds the line, repairs the script, and
> proves the repair."

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
> nothing is changed. Line 14 is where memory blew up; line 15 is why it can't be fixed.
> And it tells the person what to do instead: load only the column the median needs.
> We checked that: it runs in 107 MB with the same answer."

### 7. The tests

```bash
python3 -m pytest
```

> "98 automated tests. They cover every gate, a patch with a deliberate bug that
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
python3 -m tracepatch doctor
```
It must end with `Ready.` and show `model llama3.2 is installed`.

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
| Tests | 98 |
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
| Interactive mode misbehaves | Use the one command: `python3 -m tracepatch demo/clinvar_summary.py` |
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
