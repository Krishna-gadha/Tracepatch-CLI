"""Demo pipeline that TracePatch must REFUSE.

The median needs every value at once: the median of chunk medians is NOT the
median of the file. Naively chunking this would return a wrong answer with
no error, which is worse than crashing. Gate 2 must refuse it.

  python3 demo/unsafe_pipeline.py demo/big.csv
"""

import sys

import pandas as pd


def median_depth(path):
    df = pd.read_csv(path)
    return df["depth"].median()


if __name__ == "__main__":
    print(f"median_depth={median_depth(sys.argv[1])}")
