"""Our lab's analysis script: summarise the ClinVar variant table.

ClinVar (NCBI) is the public database of human genetic variants and what
clinical labs have reported about them. Download (real data, public):

  curl -s https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz \\
    | gunzip -c | head -n 300001 > demo/data/clinvar.tsv

Run:  python3 demo/clinvar_summary.py demo/data/clinvar.tsv

Like most quick analysis scripts, it loads the whole table into memory at once.
"""

import sys

import pandas as pd


def clinvar_summary(path):
    df = pd.read_csv(path, sep="\t", low_memory=False)
    submissions = df["NumberSubmitters"].sum()
    variants = len(df)
    return {
        "variants": variants,
        "lab_submissions": submissions,
        "submissions_per_variant": submissions / variants,
        "most_submissions_for_one_variant": df["NumberSubmitters"].max(),
        "highest_position": df["Start"].max(),
    }


if __name__ == "__main__":
    stats = clinvar_summary(sys.argv[1])
    print(", ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in stats.items()))
