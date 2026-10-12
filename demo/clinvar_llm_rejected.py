"""Evidence for the benign ClinVar records: most and fewest lab submissions.

Chunk-safe in principle (keep a running max and min), and Gate 2 accepts it.
The template rewriter does not know this pattern, so an LLM is asked. A small
local model usually gets it WRONG, and Gate 3 rejects its answer: this script
shows the verification doing its job.

    python3 -m tracepatch run demo/clinvar_llm_rejected.py --rewriter auto
"""

import pandas as pd

DATA_FILE = "demo/data/clinvar.tsv"


def benign_evidence():
    df = pd.read_csv(DATA_FILE, sep="\t")
    benign = df[df["ClinSigSimple"] == 0]
    return benign["NumberSubmitters"].max(), benign["NumberSubmitters"].min(), len(benign)


if __name__ == "__main__":
    most, fewest, records = benign_evidence()
    print("Benign records:", records)
    print("Most / fewest lab submissions:", most, "/", fewest)
