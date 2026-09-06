"""Command-line entry point for station qualification evidence."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from app.qualification.report import EnergizedAuthorization
from app.qualification.runner import QualificationRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lab-control-qualify")
    parser.add_argument("--settings", required=True)
    parser.add_argument("--output-directory", required=True)
    parser.add_argument("--simulate", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)
    passive = subparsers.add_parser("passive")
    passive.add_argument("--devices", default="")
    passive.add_argument("--read-anritsu-trace", action="store_true")
    recipe = subparsers.add_parser("recipe")
    recipe.add_argument("recipe")
    recipe.add_argument("--devices", default="")
    recipe.add_argument("--allow-energized", action="store_true")
    recipe.add_argument("--dummy-load-id", default="")
    recipe.add_argument("--interlock-confirmed", action="store_true")
    recipe.add_argument("--confirmation", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        runner = QualificationRunner(
            args.settings,
            output_directory=Path(args.output_directory),
            simulation=args.simulate,
        )
        if args.command == "passive":
            devices = tuple(item.strip() for item in args.devices.split(",") if item.strip()) or None
            report_path = runner.run_passive(
                devices=devices,
                read_anritsu_trace=args.read_anritsu_trace,
            )
        else:
            devices = tuple(item.strip() for item in args.devices.split(",") if item.strip()) or None
            report_path = runner.run_recipe(
                args.recipe,
                devices=devices,
                authorization=EnergizedAuthorization(
                    allow_energized=args.allow_energized,
                    dummy_load_id=args.dummy_load_id,
                    interlock_confirmed=args.interlock_confirmed,
                    confirmation=args.confirmation,
                ),
            )
        from app.qualification.report import QualificationReport

        status = QualificationReport.verify_file(report_path)["overall_status"]
        return 0 if status in {"passed", "simulation_passed"} else 1
    except Exception as exc:
        print(f"qualification failed before evidence completion: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
