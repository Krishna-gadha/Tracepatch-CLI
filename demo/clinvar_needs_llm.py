"""How many lab submissions back the pathogenic ClinVar records?

This filters rows first and THEN sums another column. It is still chunk-safe
(filter each chunk, sum, add up), and Gate 2 accepts it, but the template
rewriter does not know this pattern. This is what the LLM rewriter is for
(needs ANTHROPIC_API_KEY and --rewriter auto).

    python3 demo/clinvar_needs_llm.py
"""

import pandas as pd

DATA_FILE = "demo/data/clinvar.tsv"


def pathogenic_evidence():
    df = pd.read_csv(DATA_FILE, sep="\t")
    pathogenic = df[df["ClinSigSimple"] == 1]
    return len(df), pathogenic["NumberSubmitters"].sum()


if __name__ == "__main__":
    records, submissions = pathogenic_evidence()
    print("Variant records:", records)
    print("Lab submissions for pathogenic records:", submissions)
