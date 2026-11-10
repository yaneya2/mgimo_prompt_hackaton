"""Local workflow commands until the A6 business HTTP API is available."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from app.config import Settings
from app.storage import Storage, StorageError

DEFAULT_LIMITS = dict(max_model_calls=20, max_search_calls=10, max_repair_cycles=2, timeout_seconds=120, budget=None)


def main():
    parser = argparse.ArgumentParser(description="Create and inspect durable workflows; submit human input")
    parser.add_argument("--data-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--name", required=True)
    create.add_argument("--brief", required=True, type=Path, help="JSON file containing BriefInput")
    create.add_argument("--limits", type=Path, help="Optional Limits JSON file")
    status = commands.add_parser("status")
    status.add_argument("run_id")
    for name in ("answers", "select", "resume"):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        command.add_argument("--revision", required=True, type=int)
        if name == "answers":
            command.add_argument("--file", required=True, type=Path, help="JSON array of question_id/value answers")
        elif name == "select":
            command.add_argument("--option", required=True)
            command.add_argument("--version", required=True, type=int)
            command.add_argument("--rationale", required=True)
    args = parser.parse_args()
    try:
        settings = Settings(**({"data_dir": args.data_dir} if args.data_dir else {}))
        storage = Storage(settings.data_dir)
        if args.command == "create":
            brief = json.loads(args.brief.read_text(encoding="utf-8"))
            limits = json.loads(args.limits.read_text(encoding="utf-8")) if args.limits else DEFAULT_LIMITS
            from app.storage.validation import validate
            validate("Limits", limits)
            project = storage.create_project(args.name, brief)
            result = storage.create_run(project["id"], limits)
        elif args.command == "status":
            result = storage.get_snapshot(args.run_id)
        elif args.command == "answers":
            answers = json.loads(args.file.read_text(encoding="utf-8"))
            result = storage.submit_answers(args.run_id, answers, expected_revision=args.revision)
        elif args.command == "select":
            result = storage.submit_selection(args.run_id, args.option, args.version, args.rationale, expected_revision=args.revision)
        else:
            result = storage.requeue_paused(args.run_id, expected_revision=args.revision)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except StorageError as error:
        print(str(error), file=sys.stderr)
        return 2
    except (ValueError, OSError, sqlite3.Error):
        print("Command failed: check configuration, JSON input and the data directory.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
