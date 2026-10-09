"""Our lab's second script: median lab submissions per variant.

TracePatch must REFUSE this: the median of chunk medians is not the median.
"""

import sys

import pandas as pd


def median_submissions(path):
    df = pd.read_csv(path, sep="\t")
    return df["NumberSubmitters"].median()


if __name__ == "__main__":
    print("Median submissions per variant:", median_submissions(sys.argv[1]))
