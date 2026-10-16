"""How well-checked is a typical ClinVar record? Median, 90th percentile, average.

NumberSubmitters = how many labs reported on a record. A few famous records
have up to 95 reports, which pulls the average up; the median is the typical
value. TracePatch repairs this exactly with a tally (how often each value
occurs), because the median of chunk medians would be wrong.

    python3 demo/clinvar_median.py
"""

import pandas as pd

DATA_FILE = "demo/data/clinvar.tsv"


def submission_stats():
    df = pd.read_csv(DATA_FILE, sep="\t")
    median = df["NumberSubmitters"].median()
    p90 = df["NumberSubmitters"].quantile(0.9)
    average = df["NumberSubmitters"].mean()
    return median, p90, average


if __name__ == "__main__":
    median, p90, average = submission_stats()
    print("Median lab submissions per record:", median)
    print("90th percentile:", p90)
    print(f"Average: {average:.2f}")
