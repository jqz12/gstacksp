"""Portable argv entry for a project's declared architecture quality check."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gsp.architecture import main

if __name__ == '__main__':
    raise SystemExit(main())
