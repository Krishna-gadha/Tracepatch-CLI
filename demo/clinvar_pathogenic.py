"""How many ClinVar records are pathogenic (disease-causing)?

ClinSigSimple is 1 when at least one lab classified the variant as
pathogenic or likely pathogenic. Note: ClinVar lists each variant once per
genome assembly (GRCh37 and GRCh38), so these are records, not unique variants.

Counting rows that match a condition is chunk-safe: count the matches in
each chunk, then add the counts.
"""

import sys

import pandas as pd


def count_pathogenic(path):
    df = pd.read_csv(path, sep="\t")
    records = len(df)
    pathogenic = (df["ClinSigSimple"] == 1).sum()
    return records, pathogenic


if __name__ == "__main__":
    records, pathogenic = count_pathogenic(sys.argv[1])
    print("Variant records:", records)
    print("Pathogenic / likely pathogenic:", pathogenic, f"({100 * pathogenic / records:.1f}%)")
