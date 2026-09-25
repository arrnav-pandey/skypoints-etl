"""Command line entry point: ``skypoints run --members ... --redemptions ...``."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from .config import PipelineConfig
from .pipeline import run, run_sources


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an ISO date (YYYY-MM-DD)") from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skypoints",
        description="Load SkyPoints member and redemption feeds into per-country tables.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_cmd = sub.add_parser("run", help="process one daily batch")
    run_cmd.add_argument("--members", required=True, type=Path, help="member profile flat file")
    run_cmd.add_argument("--redemptions", type=Path, help="partner JSON redemption feed")
    run_cmd.add_argument("--out", type=Path, default=Path("out"), help="output directory")
    run_cmd.add_argument(
        "--as-of",
        type=_parse_date,
        help="batch date driving Age and Stale_Member; defaults to today. "
        "Pass it explicitly to make a re-run reproduce the original values.",
    )
    run_cmd.add_argument(
        "--stale-after-days",
        type=int,
        default=90,
        help="days since last flight beyond which a member is stale (default: 90)",
    )
    run_cmd.add_argument(
        "--fail-on-quarantine",
        action="store_true",
        help="exit non-zero if any record was quarantined (use in scheduled runs)",
    )

    ingest_cmd = sub.add_parser(
        "ingest",
        help="process the per-country member feeds (CSV / XLSX)",
        description=(
            "Reads one file per country, where the country is identified by the "
            "filename. Each file may have its own schema, format and date "
            "encoding; the differences are declared as source contracts."
        ),
    )
    ingest_cmd.add_argument(
        "--dir", type=Path, help="directory of country feeds, e.g. data/incoming"
    )
    ingest_cmd.add_argument(
        "--files", type=Path, nargs="*", default=[], help="individual country feeds"
    )
    ingest_cmd.add_argument("--redemptions", type=Path, help="partner JSON redemption feed")
    ingest_cmd.add_argument("--out", type=Path, default=Path("out"), help="output directory")
    ingest_cmd.add_argument("--as-of", type=_parse_date, help="batch date driving Age and Stale_Member")
    ingest_cmd.add_argument("--stale-after-days", type=int, default=90)
    ingest_cmd.add_argument("--fail-on-quarantine", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    config = PipelineConfig(
        as_of_date=args.as_of or date.today(),
        stale_after_days=args.stale_after_days,
    )

    if args.command == "ingest":
        files = list(args.files)
        if args.dir:
            files += sorted(p for p in args.dir.iterdir() if p.is_file())
        if not files:
            print("nothing to ingest: pass --dir or --files", file=sys.stderr)
            return 2
        report = run_sources(files, args.redemptions, args.out, config)
    else:
        report = run(args.members, args.redemptions, args.out, config)

    print(report.to_json())
    print(f"\nOutputs written to {args.out.resolve()}", file=sys.stderr)

    if args.fail_on_quarantine and (report.members_quarantined or report.redemptions_quarantined):
        print(
            f"FAILED: {report.members_quarantined} member and "
            f"{report.redemptions_quarantined} redemption records quarantined",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
