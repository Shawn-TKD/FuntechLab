"""Local entry point; no installation or global PYTHONPATH required."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from daydreamer_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
