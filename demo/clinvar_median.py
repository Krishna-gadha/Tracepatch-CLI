"""Median number of lab submissions per ClinVar record.

TracePatch must REFUSE this: the median of chunk medians is not the median.

    python3 demo/clinvar_median.py
"""

import pandas as pd

DATA_FILE = "demo/data/clinvar.tsv"


def median_submissions():
    df = pd.read_csv(DATA_FILE, sep="\t")
    return df["NumberSubmitters"].median()


if __name__ == "__main__":
    print("Median submissions per record:", median_submissions())
