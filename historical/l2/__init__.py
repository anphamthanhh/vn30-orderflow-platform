"""L2 observations: the Python side of the feature observation layer (ADR 0037).

``tape.py``, ``prof_tape.py`` and ``book.py`` read frozen L0 tapes into engine units, and
``producers/`` computes the features. Rust defines the features the live engine uses; a Python
port joins L2 only once a parity test (e.g. ``historical/tests/test_l2_parity_cvd.py``) matches
it. Only Python batch jobs may write L2 rows (Rust never does); the producers return rows.
"""
