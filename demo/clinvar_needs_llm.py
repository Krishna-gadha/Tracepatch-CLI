"""How many lab submissions back the pathogenic ClinVar records?

This filters rows first and THEN sums another column. It is still chunk-safe
(filter each chunk, sum, add up), and Gate 2 accepts it, but the template
rewriter does not know this pattern. This is the kind of function the LLM
rewriter is for (needs ANTHROPIC_API_KEY, --rewriter auto).
"""

import sys

import pandas as pd


def pathogenic_evidence(path):
    df = pd.read_csv(path, sep="\t")
    pathogenic = df[df["ClinSigSimple"] == 1]
    return len(df), pathogenic["NumberSubmitters"].sum()


if __name__ == "__main__":
    records, submissions = pathogenic_evidence(sys.argv[1])
    print("Variant records:", records)
    print("Lab submissions for pathogenic records:", submissions)
