"""L2 feature producers, one module per feature family.

A producer whose feature has a Rust definition joins the L2 write path only once its parity
test passes (e.g. ``historical/tests/test_l2_parity_cvd.py``).
"""
