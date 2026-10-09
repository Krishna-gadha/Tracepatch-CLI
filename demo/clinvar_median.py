"""Our lab's second script: the median number of lab submissions per variant.

TracePatch must REFUSE this one: the median of chunk medians is not the
median of the whole table, so chunking would silently give a wrong answer.

Run:  python3 demo/clinvar_median.py demo/data/clinvar.tsv
"""

import sys

import pandas as pd


def median_submissions(path):
    df = pd.read_csv(path, sep="\t", low_memory=False)
    return df["NumberSubmitters"].median()


if __name__ == "__main__":
    print(f"median_submissions={median_submissions(sys.argv[1])}")
