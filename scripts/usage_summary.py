"""Show usage totals without making model or tool calls."""
import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from app.config import Settings
from app.llm.summary import summarize_usage_files


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="JSONL journals; default: APP_DATA_DIR/usage.jsonl")
    parser.add_argument("--run-id", help="Only aggregate the selected run")
    args = parser.parse_args(argv)
    try:
        paths = args.paths or [Settings().data_dir / "usage.jsonl"]
        report = summarize_usage_files(paths, run_id=args.run_id)
    except ValidationError:
        print("Invalid application configuration", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
