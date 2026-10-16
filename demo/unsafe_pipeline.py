"""Demo pipeline that TracePatch must REFUSE.

Group-by is not something TracePatch repairs, so Gate 2 refuses it rather
than guess. (Median, percentiles and mean ARE repaired, with a tally or a
sum and a count; see demo/clinvar_median.py.)

  python3 demo/unsafe_pipeline.py demo/big.csv
"""

import sys

import pandas as pd


def depth_per_chromosome(path):
    df = pd.read_csv(path)
    return df.groupby("chrom")["depth"].sum()


if __name__ == "__main__":
    print(depth_per_chromosome(sys.argv[1]).to_dict())
