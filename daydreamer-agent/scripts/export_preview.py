"""Export a completed silent preview and its readable story without credentials."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from daydreamer_agent.application.delivery import export_delivery
from daydreamer_agent.storage.files import identifier


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = export_delivery(ROOT / "data/runs" / identifier(args.run), args.output)
    print(result["video"])


if __name__ == "__main__":
    main()
