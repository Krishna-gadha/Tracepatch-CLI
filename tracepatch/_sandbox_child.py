"""Runs INSIDE the sandbox subprocess started by gate3_sandbox.

Usage: python _sandbox_child.py <job.json>

Executes one function on the sample data and writes its return value,
normalised to JSON (never pickle), to the result file named in the job.
"""

import json
import os
import sys
import traceback

# Make `tracepatch.gate3_oracle.normalize` importable from here.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    with open(sys.argv[1]) as f:
        job = json.load(f)
    try:
        from tracepatch.gate3_oracle import normalize
        # A fresh namespace: the code sees only its own imports + CHUNKSIZE.
        namespace = {"__name__": "tracepatch_sandbox", "CHUNKSIZE": job["chunksize"]}
        exec(compile(job["source"], "<sandboxed>", "exec"), namespace)
        result = namespace[job["func_name"]](job["data_path"])
        out = {"ok": True, "result": normalize(result)}
    except Exception:
        out = {"ok": False, "error": traceback.format_exc(limit=5)}
    with open(job["result_path"], "w") as f:
        json.dump(out, f)


if __name__ == "__main__":
    main()
