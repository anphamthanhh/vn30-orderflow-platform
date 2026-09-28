"""Repository-wide pytest configuration: put the repository root on ``sys.path``.

This lets ``historical``, ``shared`` and ``data_provider`` import without installation when a
single test file is run from any directory. The Rust half of the L2 parity tests
(``cargo run -p orderflow-engine --bin l2_dump``) skips with a message when cargo is absent
and fails if the build fails; ``CARGO_TARGET_DIR`` keeps build output outside the repository.
No test uses the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
