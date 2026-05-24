from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ltx_batch.batch import BatchRunOptions, run_batch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a storyboard cell + prompt batch against a ComfyUI LTX image-to-video workflow."
    )
    parser.add_argument(
        "--config",
        default="config/workflow_config.json",
        help="Path to the batch configuration JSON.",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="Inclusive start index from prompts.json.",
    )
    parser.add_argument(
        "--end-index",
        type=int,
        help="Inclusive end index from prompts.json. Defaults to the last record.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite project output files if they already exist.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        help="Override poll interval in seconds.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        help="Override the per-job timeout in seconds.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_batch(
        options=BatchRunOptions(
            config_path=PROJECT_ROOT / args.config,
            start_index=args.start_index,
            end_index=args.end_index,
            overwrite=args.overwrite,
            poll_interval=args.poll_interval,
            timeout_seconds=args.timeout_seconds,
        ),
        on_log=print,
    )
    if summary.failed:
        raise SystemExit(
            f"{summary.failed} job(s) failed. See {PROJECT_ROOT / 'failed_jobs.json'}"
        )
    print("All selected jobs completed successfully.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc))
