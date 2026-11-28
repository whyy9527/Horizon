"""Read Xiaohongshu hot topics or open a public original note."""

import argparse
from datetime import date, datetime
import json

from .public_social_boards import BEIJING, read_board
from .public_social_works import read_work


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    hot = commands.add_parser("hot", help="Read the live board or a dated public archive")
    hot.add_argument("--date", type=date.fromisoformat)
    note = commands.add_parser("note", help="Read a note by its original or share URL")
    note.add_argument("url")
    args = parser.parse_args(argv)

    try:
        if args.command == "hot":
            today = datetime.now(BEIJING).date()
            result = read_board(("xiaohongshu", args.date or today, today, "general"))
        else:
            result = read_work("xiaohongshu", args.url)
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not isinstance(result, dict) or result.get("status", "ok") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
