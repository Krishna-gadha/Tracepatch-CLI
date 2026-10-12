from tracepatch.interactive import auto_chunk_rows, clean_path


def test_dragged_paths_are_cleaned():
    assert clean_path("'/a b/c.csv'") == "/a b/c.csv"        # quoted (VS Code)
    assert clean_path("/a\\ b/c.csv ") == "/a b/c.csv"       # escaped space (Terminal)
    assert clean_path('"/x/y.py"') == "/x/y.py"


def test_chunk_rows_follow_row_width():
    assert auto_chunk_rows(204_000_000, 300_000) == 25_000    # wide ClinVar rows
    assert auto_chunk_rows(116_000_000, 4_000_000) == 100_000  # narrow rows: capped
