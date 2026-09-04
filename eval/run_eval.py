#!/usr/bin/env python3
"""Single CLI entry point for running or finalizing the GitAgent benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from runner import EvalRunner, finalize_run


def build_run_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the GitAgent automatic evaluation suite"
    )
    parser.add_argument(
        "--dataset",
        default="eval/gitagent-evaluation-dataset.json",
        help="103-sample dataset (61 official M3-M7 cases + 22 legacy M2 cases + 20 optional E2E cases)",
    )
    parser.add_argument("--config", default="config.json", help="base GitAgent config")
    parser.add_argument("--output", required=True, help="isolated eval run directory")
    parser.add_argument("--sample", help="run one task_name:id sample")
    parser.add_argument(
        "--group",
        choices=("E2E", "M3", "M4", "M5", "M6", "M7"),
        help="run one metric group",
    )
    parser.add_argument(
        "--repetitions", type=int, default=5, help="valid M3/M4 repetitions per variant"
    )
    parser.add_argument(
        "--resume", action="store_true", help="continue an existing run directory"
    )
    parser.add_argument(
        "--keep-fixtures",
        action="store_true",
        help="preserve eval-owned fixtures for debugging",
    )
    return parser


def build_finalize_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge deterministic results with external Judge JSONL"
    )
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--judge-results", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["finalize"]:
        args = build_finalize_parser().parse_args(arguments[1:])
        metrics = finalize_run(args.run_dir, args.judge_results)
    else:
        args = build_run_parser().parse_args(arguments)
        metrics = EvalRunner(
            dataset_path=args.dataset,
            config_path=args.config,
            output_dir=args.output,
            repetitions=args.repetitions,
            sample_key=args.sample,
            group=args.group,
            resume=args.resume,
            keep_fixtures=args.keep_fixtures,
        ).run()
    print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
