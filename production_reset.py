"""Dry-run by default; guarded local production reset CLI."""

import argparse
import json

from database import engine
from production_reset_service import (
    RESET_CONFIRMATION, ProductionResetError, execute_production_reset,
    inspect_reset_plan,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Report the reset plan (default)")
    parser.add_argument("--execute", action="store_true", help="Execute the local reset after backup")
    parser.add_argument("--confirmation", help=f"Required exact text: {RESET_CONFIRMATION}")
    parser.add_argument("--backup-dir", help="Optional backup destination directory")
    args = parser.parse_args()
    if args.execute and args.dry_run:
        parser.error("Choose either --dry-run or --execute")
    try:
        if args.execute:
            result = execute_production_reset(
                engine, args.confirmation or "", backup_dir=args.backup_dir,
            )
        else:
            result = inspect_reset_plan(engine)
        print(json.dumps(result, indent=2, default=str))
        if not args.execute and not result["ready"]:
            raise SystemExit(2)
    except ProductionResetError as exc:
        print(json.dumps({"status": "refused", "error": str(exc)}, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
