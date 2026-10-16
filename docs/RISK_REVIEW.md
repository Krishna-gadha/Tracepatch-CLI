# TracePatch — Risk Review

What could go wrong with this project, and what was done about each thing.

## How this review was done

We attacked the tool the way a careful user or examiner would:

1. Wrote **25 deliberately awkward scripts and data files** (a function that
   prints instead of returning, a compressed data file, a limit that is too
   low, a wrong LLM answer, and so on) and ran TracePatch on each one.
2. Read every stage and asked "what does this assume, and what if it is false?"
3. Fixed what could be fixed, and wrote a test for each fix so it cannot come back.

**Result.** The review found, and we fixed:
- **two crashes**: compressed data files and data files that are not UTF-8;
- **one false "verified"**: functions that print their result instead of returning it;
- **three gaps in the verification**: test chunks of equal size, a patch that sets
  its own chunk size, and a sample taken only from the start of the file;
- **one bad retry**: when chunks were too big, the tool refused its own patch
  instead of trying smaller chunks;
- **a backup that could be overwritten**, and several confusing messages.

The test suite grew from 66 to 98 tests (113 after the median work of Oct 15). All 25 cases now end in one of four
clean outcomes: `VERIFIED`, `REFUSED`, `FAILED` with a reason, or `NO_PROBLEM`.
None crashes.

## The one honest statement to make

**No tool has zero limitations, and we do not claim that.** What we claim is
narrower and can be defended:

> TracePatch never accepts a repair it could not verify, and it never fails
> silently. Every case it cannot handle ends with a message that says why.

Section 9 lists the limits that remain and why they cannot be removed.

---

## 1. A wrong answer being accepted

This is the most serious kind of failure, because nobody would notice it.

| # | What could go wrong | Before | What we did | Checked by |
|---|---|---|---|---|
| 1.1 | **The function prints its result instead of returning it.** Gate 3 compared `None` with `None` and said "outputs match". | **False "verified".** | The sandbox now captures what the function prints and compares it too: numbers with the same tolerance, words exactly. | `test_printing_function_is_really_compared` |
| 1.2 | The function neither returns nor prints anything. | False "verified". | Reported as `nothing to verify`; never called verified. | `test_function_with_no_result_is_not_called_verified` |
| 1.3 | **A wrong rewrite that is only wrong when chunks are unequal**, e.g. averaging the chunk averages. With 5 equal test chunks it gave the right answer. | Would pass Gate 3. | The sandbox chunk size is now 997, which does not divide the 5,000-row sample: five full chunks and one small one. | `test_averaging_the_chunk_averages_is_caught` |
| 1.4 | The rewrite sets `CHUNKSIZE` itself (a real local-model answer did this), so the sample fits in one chunk and merging is never tested. | Would pass Gate 3. | Any patch that assigns `CHUNKSIZE` is rejected before it runs. | `test_patch_that_sets_chunksize_itself_is_rejected` |
| 1.5 | The rewrite keeps only the last chunk's result. | Caught. | Unchanged; already covered. | `test_last_chunk_bug_is_caught` |
| 1.6 | A problem that only appears late in the file. The sample was the first 5,000 rows only. | Could be missed. | The sample is now taken from **5 places across the file** (start to end). | `test_sample_is_taken_from_across_the_file` |
| 1.7 | A non-chunkable operation (sort, duplicates, group-by) is chunked. | Refused. | Unchanged: Gate 2's allowlist refuses anything not known to be safe. Since Oct 15, median, percentiles and mean are repaired with a per-chunk tally or sum and count, never from per-chunk medians or means. | `test_groupby_and_mode_refused`, `tests/test_summary_stats.py`, `test_unknown_method_refused` |
| 1.8 | Float results differ in the last digits because additions are reordered, and a correct patch is rejected. | Handled. | Unchanged: tolerance comparison (`rtol=1e-5`, `atol=1e-8`). | `test_float_reordering_accepted_although_not_exactly_equal` |
| 1.9 | The rewrite reads a different file than the original. | Not checked. | Rejected: "does not read the data file that the original reads". | `test_patch_reading_a_different_file_is_rejected` |
| 1.10 | `len(df)` came back as a numpy integer instead of a plain integer, so it printed differently inside a tuple. | Value right, type changed. | The template keeps it a plain `int`. | `test_template_keeps_len_as_a_plain_int` |
| 1.11 | A refusal leaves the person with nothing to do next. | Refused, with no guidance. | Every refusal now carries advice, e.g. for group-by: load only the needed columns. (At the time of the review this also covered median and mean; since Oct 15 both are repaired directly.) The decision itself is unchanged. | `test_refusal_comes_with_advice` |
| 1.12 | *(added Oct 15)* A median or percentile repair keeps a tally of values, and the column has almost no repeated values, so the tally is as big as the column. | — | The full-file check catches it like any other memory problem; the message says why ("one count for every DIFFERENT value") and nothing is accepted. | `test_a_tally_too_big_for_memory_fails_clearly` |
| 1.13 | *(added Oct 15)* The median or percentile from the tally differs from pandas: even or odd row counts, missing values, an empty column, uneven chunks. | — | The tally uses pandas' own method (position q × (n − 1), halfway between neighbours when needed). Checked against pandas through Gate 3 for each of those cases. | `tests/test_summary_stats.py` |

## 2. Data files

| # | What could go wrong | Before | What we did | Checked by |
|---|---|---|---|---|
| 2.1 | **The data file is compressed** (`.csv.gz`, which is how ClinVar is published). | **Crash** (`UnicodeDecodeError`). | Compression is detected from the file's first bytes (gzip, bz2, xz). The sample is read through the right decompressor and written with the same compression and name, so pandas reads it the same way. | `test_gzip_data_is_sampled_and_stays_gzip`, `test_gzip_script_is_repaired_end_to_end` |
| 2.2 | **The file is not UTF-8** (e.g. Latin-1 names). | **Crash.** | All file handling works on bytes; nothing is decoded. | `test_non_utf8_data_does_not_crash` |
| 2.3 | The file is a zip archive. | Not handled. | Refused: "zip archives are not supported; unzip the file first". | `test_zip_archive_is_refused_clearly` |
| 2.4 | A value contains a line break inside quotes, so a sample taken from the middle of the file starts in the middle of a row. | Possible failure. | If the original cannot run on the spread sample, Gate 3 falls back to the first rows before giving up. | attack case Q |
| 2.5 | Rows are very wide, so one chunk is itself too big. | Patch refused with a confusing message. | Chunk size is chosen from the row width, and halved automatically if a chunk still breaks the limit (see 5.1). | `test_chunk_size_is_halved_when_a_chunk_is_too_big` |
| 2.6 | The data file does not exist, or the script is run on the wrong file. | Long traceback. | One line: the script's own error, plus "is this the right dataset for this script?". | attack cases K, wrong-dataset run |
| 2.7 | The file is empty or small. | Handled. | `NO_PROBLEM: ... nothing to fix`. | `test_no_problem_under_cap` |
| 2.8 | The path is a web address, or uses `~`. | Not handled. | Web address: told to download the file. `~`: expanded, as pandas does. | attack case Z3 |
| 2.9 | The path is built at run time (`os.path.join(...)`). | Refused. | Still refused, safely. The message names `join()`, which is less clear than it could be. | attack case X |

## 3. The shape of the script

| # | What could go wrong | Before | What we did | Checked by |
|---|---|---|---|---|
| 3.1 | **One function loads and returns the table, another analyses it** (very common). Chunking cannot help: the caller still receives every row. | "no supported aggregation". | Explained: "returns the whole table it loads … do the calculation in the same function as the loading". | `test_function_that_returns_the_whole_table_is_explained` |
| 3.2 | **A flat script with no functions.** | Refused. | Still refused, and now says how to fix it: put the loading and calculation in a function. | `test_web_address_and_flat_script_messages` |
| 3.3 | The loading is in a method of a class. | Vague failure. | Explained: "is a method of the class X; TracePatch can only test a plain function". | `test_reasons_a_function_cannot_be_tested_on_its_own` |
| 3.4 | The function needs other arguments. | Wrong advice ("give --data"). | Explained, naming the arguments; arguments with defaults are fine. | same test |
| 3.5 | The function uses a list or dictionary defined at the top of the script. | Vague failure. | Literal values at the top of the script are now available in the sandbox. | `test_script_level_list_is_available_in_the_sandbox` |
| 3.6 | The function calls another function from the script. | Refused. | Unchanged: an unknown call is refused by Gate 2. | attack case M |
| 3.7 | Two functions each load the file. | Handled. | The retry loop repairs the second one on the next attempt. | attack case N |
| 3.8 | One function loads two big tables. | Refused its own patch, confusingly. | Fails clearly: "TracePatch repairs one load per function. Nothing was accepted". | `test_second_load_in_the_same_function_fails_clearly` |
| 3.9 | The script has a syntax error or an ordinary bug. | Long traceback. | One line with the real error. | `test_ordinary_exception_is_not_reported_as_oom` |

## 4. Finding the crash (Gate 1)

| # | What could go wrong | Before | What we did | Checked by |
|---|---|---|---|---|
| 4.1 | The process is killed and all evidence is lost. | Handled. | Each sample is flushed to a file, so the record survives the kill. | `test_hog_is_killed_at_the_right_line` |
| 4.2 | Memory grows faster than one 50 ms sample, so no line is recorded. | Reported line 0. | Reports "unknown line"; never guesses. | `test_no_in_script_sample_means_unknown_line_not_zero` |
| 4.3 | **The limit is lower than Python and pandas need to start.** | Misleading "module level" refusal. | Explained: the limit was reached while importing libraries; use a higher limit. | `test_limit_below_what_python_needs_is_explained` |
| 4.4 | The script exceeds the limit by a little before the watchdog reacts. | Known. | The watchdog checks every 10 ms. "Memory before" is reported as a lower bound, not the true need. | stated in the output |
| 4.5 | The script starts other processes that use the memory. | Not measured. | **Remaining limit**: only the script's own process is measured. | section 9 |

## 5. Memory after the repair

| # | What could go wrong | Before | What we did | Checked by |
|---|---|---|---|---|
| 5.1 | **The repair gives the right answer but one chunk is still too big.** | Ended by refusing its own patch. | The chunk size is halved and only the full-scale check is repeated, because the answer was already verified. Stops at 3 attempts or 1,000 rows. | `test_chunk_size_is_halved_when_a_chunk_is_too_big` |
| 5.2 | The repair passes the sample test but still loads everything. | Handled. | The full-scale check runs the whole repaired script with the tracer attached. | `test_retry_loop_stops_after_three_attempts` |
| 5.3 | The retry loop never ends. | Handled. | Hard cap of 3 attempts; it cannot be raised. | `test_attempt_cap_cannot_be_raised` |
| 5.4 | The repaired script is very slow on a huge file. | Vague failure. | Reported as a timeout, with the option to allow more time. | code path in the orchestrator |
| 5.5 | **The repair fits, but only just.** Memory varies a few percent between runs, so a script verified at just under the limit was killed on a later run (seen on a slower test machine). | Accepted. | The full-scale check wants the peak at or below 90% of the limit. If it is closer, it halves the chunk size and checks again; on the last attempt a tight fit is accepted with a note. | `HEADROOM` in the orchestrator; the automatic macOS runs |
| 5.6 | The kill is recorded one line after the chunk is read (sampling is every 50 ms), inside the loop body. | Treated as a new fault; misleading message. | Any line inside the chunk loop counts as "a chunk is too big". | `test_a_kill_anywhere_in_the_chunk_loop_counts_as_a_chunk_that_is_too_big` |

## 6. Rewriting, and the LLM

| # | What could go wrong | Before | What we did | Checked by |
|---|---|---|---|---|
| 6.1 | **The LLM writes code that does something dangerous** (opens files, runs commands, imports modules). | It would have been run in the sandbox. | Rewritten code is checked against a fixed vocabulary **before it is run**. `open`, `eval`, `exec`, `__import__`, `to_csv`, extra imports and extra statements are rejected unrun. | `test_unsafe_patch_is_rejected_without_running` (the test proves the file was never created) |
| 6.2 | The LLM writes a wrong answer. | Handled. | Gate 3 rejects it; the reason is sent back; at most 3 attempts. | demo: `clinvar_llm_rejected.py` |
| 6.3 | The LLM returns text that is not Python. | Run ended. | Rejected, and the reason is sent back for another attempt. | `test_bad_model_answer_is_retryable_but_cannot_chunk_is_not` |
| 6.4 | The LLM adds placeholders (`TODO`, `...`). | Handled. | Rejected before running. | `test_placeholder_rejected` |
| 6.5 | The local model is not running or not installed. | Misleading message. | Clear messages for both, including the command to install the model. | `test_local_rewriter_unreachable_server_fails_clearly` |
| 6.6 | The API key leaks into generated code. | Handled. | The sandbox does not pass secrets to the code it runs. | `test_secrets_are_not_inherited` |
| 6.7 | The template meets a pattern it does not know. | Handled. | It refuses instead of guessing, and says an LLM can try. | `test_template_refuses_unsupported_use_of_df` |

## 7. Applying the repair

| # | What could go wrong | Before | What we did | Checked by |
|---|---|---|---|---|
| 7.1 | The original script is lost. | Handled. | The original is kept as `SCRIPT.orig`; `restore` puts it back. | `test_apply_replaces_script_and_keeps_backup` |
| 7.2 | Applying twice overwrites the backup with an already-repaired version. | **The true original would be lost.** | An existing backup is never overwritten. | `test_apply_never_overwrites_the_true_original` |
| 7.3 | The repair is applied before it is verified. | Never. | Applying only happens after every check has passed. | by construction |

## 8. Using it

| # | What could go wrong | Before | What we did | Checked by |
|---|---|---|---|---|
| 8.1 | A dragged-in path has spaces or quotes. | Handled. | Cleaned correctly. | `test_dragged_paths_are_cleaned` |
| 8.2 | The user types a wrong path or a non-number. | Handled. | Asked again. | manual runs |
| 8.3 | Ctrl+C in the middle. | Handled. | Exits cleanly. | manual run |
| 8.4 | It only works on one operating system. | Tested on macOS only. | The tests now run automatically on Linux and macOS for every change (GitHub Actions). Windows is untested. | `.github/workflows/tests.yml` |
| 8.5 | **The tests themselves only pass on our laptop.** They used a fixed 200 MB limit; on Linux with a newer pandas the same file needs 130–198 MB, so nothing was killed and 13 tests failed. | Found by the first automatic run. | The tests measure the memory limit on the machine they run on (`tests/conftest.py`). | the Linux and macOS runs: all pass |
| 8.6 | The tests pass on one run and fail on the next, because memory varies between runs. | Seen on the macOS test machine. | The tests measure the peak twice and keep the lower value, and use a 2,000,000-row file so the gap is much larger than the variation. | two clean runs in a row on all three machines |
| 8.7 | Someone installs it and the AI model is missing, with no idea why repairs fail. | A hint only when a repair failed. | `tracepatch doctor` checks the libraries, Ollama and the model, and prints the exact fix. | `test_doctor_says_how_to_get_the_model` |

---

## 9. What remains, and why

These cannot be removed. They can only be stated, and each is guarded.

| Limit | Why it cannot be removed | What protects the user |
|---|---|---|
| **The LLM is a small local model; Claude has not been run live.** | A stronger model needs a paid key. | The gates do not depend on the model being good. A wrong answer is rejected (shown live). |
| **Checking on a sample is not a proof.** | The original cannot finish on the full file, so there is no full answer to compare with. Nothing can check against an answer that cannot be computed. | The sample comes from across the file, in uneven chunks, and the full-scale run checks memory for real. |
| **Only operations known to be chunk-safe are repaired.** Sort, group-by, duplicates and anything unrecognised are refused. (Median, percentiles and `mean()` are repaired since Oct 15; a median or percentile only saves memory when values repeat.) | Guessing here gives silent wrong answers. Refusing is the point. | The refusal names the operation and the line. |
| **The loading must be inside a function that returns or prints its result.** Flat scripts and functions that return the whole table are refused. | The function is the unit that can be run on a sample and compared. | The message says how to restructure the script. |
| **One load per function.** | The rewrite handles one `read_csv` at a time. | Fails clearly; nothing is accepted. |
| **Only the script's own process is measured.** | Scripts that start other processes are outside what the tracer sees. | Stated. |
| **The sandbox is not a security boundary.** | That needs containers. | Rewritten code is checked against a fixed vocabulary before it runs, and runs with no secrets, a time limit and a memory limit. |
| **CSV/TSV tables with pandas only.** | Raw sequencing formats use different tools. | Stated. |
| **No study with real users.** | Outside a course project. | Real-world context is taken from public documentation, with sources (learning guide, section 21). |

---

## 10. The 25 attack cases

| # | Case | Outcome |
|---|---|---|
| A | Function prints instead of returning | VERIFIED (printed output compared) |
| B | Loading inside a class method | FAILED, explained |
| C | Loading inside a nested function | VERIFIED |
| D | Function needs an extra argument | FAILED, explained |
| E | Function uses a list defined in the script | VERIFIED |
| F | Data file is gzip-compressed | VERIFIED |
| G | Data file is Latin-1, not UTF-8 | VERIFIED |
| H | Limit below what Python needs | FAILED, explained |
| I | Syntax error in the script | FAILED, the script's own error |
| J | Memory used without `read_csv`, very fast | FAILED, "unknown line" |
| K | Data file missing | FAILED, the script's own error |
| L | Empty data file | NO_PROBLEM |
| M | Function calls a helper function | REFUSED |
| N | Two functions both load the file | VERIFIED |
| O | `read_csv` with extra options | VERIFIED (options kept) |
| Q | Quoted values containing line breaks | VERIFIED |
| U | `apply` with a lambda | REFUSED |
| X | Path built at run time | REFUSED |
| Y | Chunk too big for the limit | VERIFIED after two automatic halvings |
| Z1 | A function that returns the whole table | FAILED, explained |
| Z2 | Two big loads in one function | FAILED, explained |
| Z3 | Data read from a web address | FAILED, the script's own error |
| Z4 | Flat script, no functions | REFUSED, with how to fix |
| Z5 | `mean()` of a column | REFUSED at the time of the review; VERIFIED since Oct 15 (sum and count per chunk) |
| Z6 | `groupby` | REFUSED |

For every VERIFIED case, the repaired script's output was compared with the
original script run with no limit: the values are identical.
