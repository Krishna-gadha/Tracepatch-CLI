"""Lab submissions per gene in ClinVar.

TracePatch must REFUSE this: it does not repair group-by, and it never
guesses. It names the line and suggests what to do instead.

    python3 demo/clinvar_by_gene.py
"""

import pandas as pd

DATA_FILE = "demo/data/clinvar.tsv"


def submissions_per_gene():
    df = pd.read_csv(DATA_FILE, sep="\t")
    return df.groupby("GeneSymbol")["NumberSubmitters"].sum()


if __name__ == "__main__":
    print("Genes with the most lab submissions:")
    print(submissions_per_gene().sort_values(ascending=False).head(5).to_string())
