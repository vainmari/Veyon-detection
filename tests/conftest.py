"""
conftest.py — repo root
Adds the repo root to sys.path so `import app.*` works when pytest is run
from any working directory.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))