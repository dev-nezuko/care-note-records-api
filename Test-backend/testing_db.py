"""Point the app at a throwaway SQLite file; import this before ``main`` in tests."""

import atexit
import os
import tempfile
from pathlib import Path

_directory = tempfile.TemporaryDirectory(prefix='care-notes-tests-')
os.environ['CARE_NOTES_DB_PATH'] = str(Path(_directory.name) / 'test.db')
atexit.register(_directory.cleanup)
