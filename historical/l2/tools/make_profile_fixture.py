"""SHA-256 helpers that pin the synthetic TPO price-tape fixture to its manifest.

The fixture is generated deterministically by ``samples/generate-fixtures.py``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = REPO_ROOT / "historical" / "tests" / "fixtures" / "l2_prof_tpo_synthetic_20210304_20210305.csv"
MANIFEST = FIXTURE.with_suffix(".manifest.json")


def normalised_bytes(path: Path) -> bytes:
    """Fixture bytes with CRLF normalised to LF, so the hash does not depend on the checkout."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(normalised_bytes(path)).hexdigest()
