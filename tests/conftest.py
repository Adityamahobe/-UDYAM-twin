"""Point the whole test run at a throwaway SQLite file so tests never write to udyam_twin.sqlite.

This runs before any test module imports api.db / api.app, which read UDYAM_DB_PATH at import.
"""

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="udyam-test-"))
os.environ["UDYAM_DB_PATH"] = str(_TMP / "test.sqlite")
