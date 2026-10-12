"""Check a ClinVar release: how many records, and how much lab evidence?

ClinVar (NCBI) is the public database of human genetic variants and what
clinical labs report about them. Run from the project folder:

    python3 demo/clinvar_summary.py
"""

import pandas as pd

DATA_FILE = "demo/data/clinvar.tsv"


def clinvar_summary():
    df = pd.read_csv(DATA_FILE, sep="\t")
    records = len(df)
    submissions = df["NumberSubmitters"].sum()
    most = df["NumberSubmitters"].max()
    return records, submissions, most


if __name__ == "__main__":
    records, submissions, most = clinvar_summary()
    print("Variant records:", records)
    print("Lab submissions:", submissions)
    print("Average submissions per record:", round(submissions / records, 2))
    print("Most submissions for one record:", most)
