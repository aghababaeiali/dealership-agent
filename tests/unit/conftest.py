"""Make data/scripts importable in tests.

Those scripts live outside src/dealership_agent (they're one-off data
pipeline tools, not application code) so they aren't on sys.path by
default.
"""

import sys
from pathlib import Path

DATA_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "data" / "scripts"
if str(DATA_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(DATA_SCRIPTS_DIR))
