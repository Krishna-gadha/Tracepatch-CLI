# TracePatch — Panel Preparation

Everything you need to explain the project to a panel: who has the problem, what
the problem is, what exists already, how we differ, the dataset, and why it is
relevant. Read it top to bottom once, then use section 9 to test yourself.

How each part of the code works is in [LEARNING_GUIDE.md](LEARNING_GUIDE.md).
What can go wrong is in [RISK_REVIEW.md](RISK_REVIEW.md). What to type is in
[DEMO_RUNBOOK.md](DEMO_RUNBOOK.md).

---

## 1. The 60-second version

> Scientists analyse large data tables with short Python scripts. Those scripts
> usually load the whole table into memory. On a shared computing cluster every
> job has a memory limit, and when a script goes over it the system kills it,
> with no error message and no line number.
>
> Today people respond by asking for more memory and running it again, or by
> rewriting the script by hand or with an AI assistant, with nothing to tell them
> whether the rewritten script still gives the same answer.
>
> TracePatch takes the script, finds the line that used too much memory, checks
> from the code whether the calculation can safely be done piece by piece,
> rewrites it, and accepts the rewrite only after proving two things: the answer
> is the same, and the memory really fits. If it is not safe, it refuses.

**One sentence:** *verified automatic repair of out-of-memory crashes in data scripts.*

---

## 2. The world this lives in

### Who

A **bioinformatician**: a person who writes programs to analyse biological data.
They work in hospital genetics labs, research institutes and universities. Most
are biologists who learned to program, or programmers who learned biology. They
write many short scripts, quickly, in Python (with the **pandas** library) or R.

### What they are doing

1. A machine reads a person's DNA (**sequencing**).
2. Programs compare it with a standard human DNA sequence (the **reference
   genome**) and list the places where it differs. Each difference is a **variant**.
   One person has millions of them; almost all are harmless.
3. To find the few that matter, each variant is **looked up in reference tables**
   that say what is already known about it. This step is called **annotation**.
4. The scientist then **analyses tables**: counts, sums, filters, summaries.
   This is the step TracePatch is about.

### Where the scripts run

Not on a laptop. Institutes have a shared **cluster**: many powerful computers
that everyone shares. You do not log in and run things directly. You write a
small **job** file saying what to run and **how much memory you need**, and hand
it to a program called the **scheduler** (a very common one is **Slurm**):

```
sbatch --mem=4G my_job.sh
```

The scheduler runs it when a machine is free. Because the machines are shared,
**the limit is enforced**: if your job uses more than it asked for, the
scheduler kills it.

### The data

Tables with millions of rows, stored as text files where each line is a row and
columns are separated by commas (**CSV**) or tabs (**TSV**). They grow every time
the source database is updated.

---

## 3. The problem, precisely

### What goes wrong

```python
df = pd.read_csv("clinvar.tsv", sep="\t")   # loads the WHOLE table into memory
```

1. `read_csv` copies the entire file from disk into **RAM** (memory).
2. In RAM the data takes **several times more space** than on disk, because each
   text value becomes a separate Python object. Our 195 MB file needs **678 MB**.
3. The job goes over its memory limit.
4. The system sends it a **SIGKILL**. A program cannot catch or respond to this
   signal. It simply stops.
5. Because it stopped instantly, **Python never gets to print an error**. There
   is no traceback and no line number. The exit code is **137** (128 + 9; 9 is
   the number of SIGKILL). Slurm's log says "oom-kill event".

OOM means **out of memory**.

### Why this is hard

- **The person does not know which line did it.** A script may load several
  tables and do many steps.
- **It worked last month.** The table grew; the script did not change.
- **The obvious fix is risky.** Reading the file in pieces (**chunks**) is correct
  for some calculations and silently wrong for others:

  ```
  whole file:  [1, 2, 3, 4, 100]      median = 3
  chunk 1:     [1, 2, 3]              median = 2
  chunk 2:     [4, 100]               median = 52
  median of the chunk medians = 27    WRONG, and no error is shown
  ```

  A sum works in pieces. A median, a sort, or removing duplicates does not. A
  wrong answer with no error is worse than a crash, because nobody notices.

### What it costs

Each failed job is wasted cluster time and another wait in the queue. Asking for
more memory works until the table grows again, and a machine only has so much.
Rewriting by hand takes time and can quietly change the result.

---

## 4. What exists already, and how we differ

**Say this first:** we do not claim to be the first or the only tool in this
area. We claim that we did not find one that **verifies** an automatic repair.

### A. Ask for more memory and run again

| What | Fact |
|---|---|
| **Slurm** (scheduler) | Kills a job that exceeds its memory request and reports an "oom-kill event". University help pages tell users to check how much the job used and request more. |
| **Nextflow** (workflow tool, very common in genomics) | Has a documented recipe: retry the task with more memory when the exit status is 137–140, e.g. `memory { 2.GB * task.attempt }`. |
| **Snakemake** (workflow tool) | The same idea: `mem_mb=lambda wildcards, attempt: 10000 * attempt`. |

**Limit:** the script is not fixed, the cause is never found, memory has a
ceiling, and every retry waits in the queue again.
**We differ:** we repair the script so it fits in the memory it already had.

### B. Memory profilers (they measure, they do not repair)

| Tool | What it does | What it does not do |
|---|---|---|
| **memory_profiler** | Shows memory use line by line. | No repair. |
| **Scalene** | Profiles CPU, GPU and memory line by line. **It can also ask an AI model to propose optimizations**, including with local models through Ollama. | It proposes; it does not check that the proposal gives the same answer or fits in memory. |
| **Fil** | Built for data scientists. Finds the peak memory and where it came from. **It can write a report at the moment memory runs out.** | No repair. Linux and macOS only. |
| **memray** (Bloomberg) | Tracks every allocation, including inside native libraries. | No repair. |

**Be honest about these two:**
- *Scalene already combines profiling with AI suggestions.* Our difference is
  not "AI". It is **verification**: a default-deny safety check before any
  rewrite, a comparison against the original afterwards, and a real memory
  measurement on the full file.
- *Fil can already tell you where memory ran out.* Our Gate 1 does a similar
  job more simply. Our contribution starts after that point.

### C. Engines built for data larger than memory

| Tool | What it does |
|---|---|
| **pandas itself** | Its user guide has a page, "Scaling to large datasets", recommending `usecols`, smaller data types, chunking, or another library. |
| **Dask** | A pandas-like library that works on data larger than memory. Recommended by the pandas guide. |
| **Polars**, **DuckDB** | Fast engines that can process a file without loading all of it. |

**Limit:** you rewrite your script in a different library, and nothing tells you
whether the new script gives the same result as the old one.
**We differ:** the script stays in pandas, a few lines change, and the result is
checked against the original.
**Be honest:** for someone starting a new large analysis, Polars or DuckDB is
probably the better choice. TracePatch is for the script that already exists.

### D. AI coding assistants (ChatGPT, Copilot, Claude)

Ask "make this script use less memory" and you get a rewrite in seconds.
**Limit:** nothing guarantees it is correct or that it actually fits. Our own demo
shows a model writing a confident, wrong rewrite.
**We differ:** we treat the AI's output as untrusted and verify it. This is the
most timely part of the project.

### E. Research: automated program repair

There is a research field on fixing programs automatically. Most of it checks a
fix by running the program's **tests**.
**The gap for us:** analysis scripts usually have no tests.
**What we do instead:** we use the **original script as the judge**. Run the old
and the new version on the same sample and compare. This is called
**differential testing**.

### The comparison in one table

| | Finds the line after a kill | Repairs the script | Checks the answer is unchanged | Checks memory on the full file | Refuses unsafe cases |
|---|---|---|---|---|---|
| More memory + retry | no | no | – | – | – |
| Fil (profiler) | yes | no | – | – | – |
| Other profilers | measure memory | no | – | – | – |
| Scalene with AI | measures | suggests | no | no | no |
| Dask / Polars / DuckDB | – | you rewrite it | no | – | – |
| AI assistant | no | suggests | no | no | no |
| **TracePatch** | **yes** | **yes** (simple cases, or with an LLM) | **yes** (on a sample) | **yes** | **yes** |

---

## 5. What TracePatch does

One command: `python3 -m tracepatch script.py`

| Stage | Question it answers | How |
|---|---|---|
| **Gate 1** | Where did it die? | Runs the script under a memory limit. A thread writes the current line and memory to a file every 50 ms, flushed immediately, so the record survives the kill. |
| **Gate 2** | Is it safe to chunk? | Reads the code of that function and checks every operation against a list of ones known to be safe (**allowlist**). Anything not on the list is refused. |
| **Rewrite** | What is the repaired code? | Built-in rules for simple cases (sum, count, min, max, counting matches). Otherwise a language model writes it. |
| **Gate 3** | Is the answer the same? | Runs the old and the new function on 5,000 rows from across the file, in isolated processes, and compares what they return and print. |
| **Verify** | Does it really fit? | Runs the whole repaired script on the full file under the same limit. If a chunk is too big, halves the chunk size and tries again. At most 3 attempts. |

Only when everything passes is the script replaced. The original is kept.

---

## 6. The dataset

### ClinVar

- **What it is:** a free, public database run by **NCBI**, the National Center
  for Biotechnology Information, part of the US National Institutes of Health.
- **What it contains:** for each known human genetic variant, what clinical
  laboratories have reported about it: whether it causes disease
  (**pathogenic**), is harmless (**benign**), or is not yet understood
  (**uncertain significance**).
- **Who uses it:** genetics labs look up a patient's variants in it. It is one
  of the standard reference tables in the field.
- **How it is published:** as downloadable files. The one we use,
  `variant_summary.txt.gz`, is a tab-separated table. NCBI states it is
  **generated weekly** and archived monthly. So labs really do re-download it,
  and it really does keep growing.

### Our copy

- The **first 300,000 rows** of that file: 195 MB on disk, 43 columns.
- The full file is 455 MB compressed. We use a part of it so the demo runs in
  seconds; the method does not depend on the size.
- It is not stored in the repository (data files are never committed). The
  README has the download command.

### The columns we use (NCBI's own definitions)

| Column | Meaning |
|---|---|
| `NumberSubmitters` | "number of submitters describing this variant": how many labs reported it. More submitters means more evidence. |
| `ClinSigSimple` | 1 if at least one current record says pathogenic or likely pathogenic; 0 if none does; -1 if there is no classification at all. |
| `ClinicalSignificance` | the classification in words (Pathogenic, Benign, Uncertain significance …). |
| `GeneSymbol` | the gene(s) the variant overlaps. |

**One fact to state correctly:** the file reports each variant **once for each
genome assembly** (GRCh37 and GRCh38, two versions of the reference genome). So
300,000 rows are 300,000 **records**, not 300,000 different variants. That is
why our scripts print "Variant records".

### What our scripts compute, and why

| Script | Computes | Why someone would run it |
|---|---|---|
| `clinvar_summary.py` | number of records, total lab submissions, average and maximum per record | a check on each new download: is it complete, and how much evidence does it hold? |
| `clinvar_pathogenic.py` | how many records are pathogenic (115,014 of 300,000; 38.3%) | how much of the table is clinically significant |
| `clinvar_median.py` | the median submissions per record | a normal statistic that **cannot** be chunked; shows the refusal |
| `clinvar_needs_llm.py` | lab submissions for pathogenic records only | filter, then sum: needs the language model |
| `clinvar_llm_rejected.py` | most and fewest submissions among benign records | the model gets it wrong; shows the rejection |

### Why this dataset

1. It is **real**, public and from an authoritative source.
2. It is a **table**, which is what these scripts load.
3. It is **wide** (43 text columns), which is what makes memory blow up.
4. It is **updated weekly and growing**, which is exactly why a script that
   worked last month fails this month.

### What we do not claim

- No biological finding. We use ClinVar as a real, large table.
- Our scripts are simple on purpose. They stand for the first step of a real
  analysis, and their answers are easy to check by eye.
- For automated tests we use generated data, so the tests need no download.

---

## 7. Why it is relevant

- **The problem is common.** Out-of-memory kills are routine enough that the
  main workflow tools ship a built-in recipe for retrying with more memory.
- **The data keeps growing.** Reference tables are updated continuously;
  sequencing gets cheaper every year.
- **The people affected are not software engineers.** They need the line number
  and a safe fix, not a profiler report to interpret.
- **AI-written code is now normal.** People paste AI rewrites into their
  pipelines. A way to check such a rewrite before trusting it is useful well
  beyond this one problem.
- **It is not limited to genomics.** Any field that analyses large tables with
  pandas has the same problem. We chose genomics because the files are large,
  public and realistic.

**What we do not claim:** that scientists use TracePatch today. Nobody does. It
is a working prototype of a method.

---

## 8. The evidence we can show

| Claim | Evidence |
|---|---|
| It finds the line | Live: `KILLED ... fault at line 15`, with no traceback available |
| It repairs correctly | Output identical to the original run with no limit |
| It saves memory | 678 MB → about 180 MB on the ClinVar file |
| It refuses unsafe cases | Live: the median script |
| It verifies AI-written code | Live: one model rewrite verified, one rejected |
| It is tested | 98 automated tests, run automatically on Linux and macOS |
| We tried to break it | 25 awkward cases; none crashes (RISK_REVIEW.md) |

---

## 9. Questions a panel will ask

**Q: Who exactly has this problem?**
People who analyse large data tables with scripts on shared clusters:
bioinformaticians first, but anyone using pandas on big files.

**Q: How do you know it is a real problem? Did you ask anyone?**
We did not survey users, and we say so. The evidence is public documentation:
Slurm reports "oom-kill events", and Nextflow and Snakemake both document
retrying with more memory on exit code 137.

**Q: What do people do about it today?**
Ask for more memory and rerun; rewrite by hand; switch to Dask, Polars or
DuckDB; or ask an AI assistant. None of these checks that the answer is unchanged.

**Q: Scalene already suggests optimizations with AI. How are you different?**
Scalene measures and suggests. It does not check the suggestion. We check
before (is this calculation safe to chunk at all?) and after (same answer on a
sample, memory measured on the full file), and we refuse when we cannot verify.

**Q: Fil already shows where memory ran out. Why build Gate 1?**
Fil is the more capable profiler. Gate 1 is a small, simple tracer that gives
us the one thing we need: the line. Our contribution is what happens after.

**Q: Why not just tell people to use Polars or DuckDB?**
For new work, that is good advice. But existing scripts would have to be
rewritten, and nothing confirms the rewritten script gives the same result.
TracePatch keeps the script in pandas and verifies the change.

**Q: Why not just give it more memory?**
It works until the table grows again, it has a ceiling, it costs queue time,
and the script stays broken.

**Q: What is new here? Chunking is in the pandas manual.**
Chunking is not new. The pipeline around it is: locating the line after a kill,
refusing unsafe operations by default, and accepting a rewrite only after
comparing it with the original and measuring its memory.

**Q: Where is the AI in an AI and ML project?**
A language model writes the repairs our rules cannot. But the point of the
project is the opposite of trusting AI: it shows how to make AI-written code
safe to accept. We demonstrate a model being wrong and being caught.

**Q: Which model, and why not a bigger one?**
`llama3.2`, a small model running on the laptop through Ollama: free and
offline. A stronger model would succeed more often, but the design does not
depend on the model being right.

**Q: Why ClinVar?**
It is real, public, authoritative, a table, wide, and updated weekly, so it
grows, which is exactly the situation that breaks scripts.

**Q: Your file is 195 MB and your laptop has 24 GB. Is the crash real?**
The limit is simulated by our watchdog, the same way a cluster scheduler
enforces a job's memory request. On a real cluster the scheduler does the
killing. Without the limit this script needs 678 MB.

**Q: Why only 300,000 rows?**
So the demo runs in seconds. The full file is 455 MB compressed. The method does
not depend on the size: the repaired script uses about the same memory however
long the file is.

**Q: How do you know the answer is right on the full file?**
We do not prove it. We compare on 5,000 rows taken from across the file, and
separately we checked the demo results against plain pandas with no limit. On
the full file under the limit, the original cannot finish, so there is nothing
to compare with. That is a stated limit.

**Q: What can it not do?**
Median, sort, group-by, `mean()` and anything it does not recognise are
refused. The loading must be inside a function. One load per function. Tables
only, with pandas. Windows is untested.

**Q: Is refusing not a weakness?**
It is the design. A tool that guesses gives silent wrong answers. Ours says
which operation it could not handle, on which line, and what to do instead. For
a median it suggests loading only the needed column; we checked that on ClinVar
and it runs in 107 MB instead of being killed. For a mean it suggests writing it
as a sum divided by a count, which TracePatch can then repair.

**Q: Is the AI model part of your tool?**
TracePatch installs as one package (`pip install .`). The model is not inside it,
because a model is about 2 GB; it is installed once through Ollama and found at
run time. `tracepatch doctor` checks that it is there. Without it, TracePatch
still does every repair its built-in rules cover.

**Q: Is anyone using it?**
No. It is a prototype. The next step would be to try it on real scripts from a
lab.

**Q: What would make it a product?**
More kinds of calculations (group-by, filters, joins against small tables);
reading the real limit from the cluster instead of simulating one; running
automatically when a job is killed; and trials with real users.

**Q: What did you find hardest?**
The cases where a check passed but was not really checking: a function that
prints instead of returning was "verified" with nothing compared; equal-sized
test chunks hid a wrong formula. We found these by attacking our own tool.

**Q: How do you know your own tool works?**
98 automated tests, run automatically on Linux and macOS for every change, and
a risk review of 25 awkward cases.

---

## 10. Words you must be able to define

| Word | Meaning |
|---|---|
| **Genome** | all of an organism's DNA |
| **Reference genome** | the standard human DNA sequence others are compared with |
| **Assembly (GRCh37, GRCh38)** | a version of the reference genome; positions differ between versions |
| **Sequencing** | reading a person's DNA with a machine |
| **Variant** | a place where a person's DNA differs from the reference |
| **Pathogenic / benign** | causes disease / harmless |
| **Annotation** | looking up what is known about each variant |
| **ClinVar** | NCBI's public database of variants and their clinical meaning |
| **NCBI** | US National Center for Biotechnology Information |
| **Bioinformatician** | someone who writes programs to analyse biological data |
| **Pipeline** | a series of analysis steps run one after another |
| **Cluster** | many shared computers used for heavy jobs |
| **Scheduler (Slurm)** | the program that decides when and where each job runs, and enforces its limits |
| **Job** | one piece of work handed to the scheduler |
| **RAM / memory** | where a running program keeps its working data |
| **RSS** | how much RAM a program is using right now |
| **OOM** | out of memory |
| **SIGKILL / exit 137** | the system force-stopping a program; 137 = 128 + 9 |
| **Traceback** | Python's error report with line numbers; absent after a kill |
| **pandas / DataFrame** | the Python library for tables / a table in it |
| **CSV / TSV** | text table files with comma / tab between columns |
| **Chunk** | a piece of the file, a fixed number of rows, read at a time |
| **Decomposable (chunk-safe)** | can be computed per chunk and merged into the exact answer |
| **AST** | the structure of code, read without running it |
| **Allowlist (default-deny)** | only listed things are allowed; everything else is refused |
| **Sandbox** | an isolated process for running code we do not trust |
| **Differential testing** | run two versions on the same input and compare |
| **Oracle** | the part that decides whether two results are the same |
| **Tolerance** | how much two numbers may differ and still count as equal |
| **LLM** | large language model: an AI that writes text and code |
| **Ollama** | a program that runs such models on your own computer |
| **Profiler** | a tool that measures where a program spends time or memory |
| **CI** | automatic testing on every change |

---

## 11. Sources

- ClinVar file descriptions and update schedule: NCBI, https://ftp.ncbi.nlm.nih.gov/pub/clinvar/README.txt
- Slurm out-of-memory kills: MSU ICER, "SLURM Job Failure Due to the oom Error", https://icer.msu.edu/for-hpcc-users/topic-of-the-month/slurm-job-failure-due-to-the-oom-error
- Nextflow retry with more memory: Seqera, "Error recovery and automatic resources management", https://seqera.io/blog/error-recovery-and-automatic-resources-management/
- nf-core troubleshooting: https://nf-co.re/docs/usage/troubleshooting/crash_halfway
- Snakemake attempt-based memory (a pipeline's documentation): https://54gene-wgs-germline.readthedocs.io/en/stable/execution.html
- pandas, "Scaling to large datasets": https://pandas.pydata.org/docs/user_guide/scale.html
- Scalene profiler: https://pypi.org/project/scalene/
- Fil profiler: https://pythonspeed.com/fil/docs
