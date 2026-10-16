import sys
import pandas as pd


def depth_per_chromosome(path):
    df = pd.read_csv(path)
    return df.groupby("chrom")["depth"].sum()


if __name__ == "__main__":
    print(depth_per_chromosome(sys.argv[1]).to_dict())
