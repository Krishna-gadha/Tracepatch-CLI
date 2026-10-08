import sys
import pandas as pd


def median_depth(path):
    df = pd.read_csv(path)
    return df["depth"].median()


if __name__ == "__main__":
    print(median_depth(sys.argv[1]))
