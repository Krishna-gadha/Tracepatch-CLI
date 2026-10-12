"""Our lab's analysis script: summarise the ClinVar variant table (NCBI).

Download (real public data):
  curl -s https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz \\
    | gunzip -c | head -n 300001 > demo/data/clinvar.tsv
"""

import sys

import pandas as pd


def clinvar_summary(path):
    df = pd.read_csv(path, sep="\t")
    variants = len(df)
    submissions = df["NumberSubmitters"].sum()
    most = df["NumberSubmitters"].max()
    return variants, submissions, most


if __name__ == "__main__":
    variants, submissions, most = clinvar_summary(sys.argv[1])
    print("Variant records:", variants)
    print("Lab submissions:", submissions)
    print("Average submissions per record:", round(submissions / variants, 2))
    print("Most submissions for one record:", most)
