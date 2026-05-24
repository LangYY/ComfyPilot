from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ltx_batch.storyboard import split_storyboard


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split a storyboard PNG into equal-sized cell images."
    )
    parser.add_argument(
        "storyboard",
        nargs="?",
        default="storyboard_3x4.png",
        help="Path to the storyboard PNG. Defaults to storyboard_3x4.png.",
    )
    parser.add_argument("--rows", type=int, default=4, help="Number of rows.")
    parser.add_argument("--cols", type=int, default=3, help="Number of columns.")
    parser.add_argument(
        "--margin",
        type=float,
        default=0,
        help="Outer margin in pixels applied to all sides.",
    )
    parser.add_argument(
        "--gutter",
        type=float,
        default=0,
        help="Gap between cells in pixels for both axes.",
    )
    parser.add_argument(
        "--output-dir",
        default="cells",
        help="Directory where 01.png to 12.png will be written.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results = split_storyboard(
        storyboard_path=Path(args.storyboard),
        output_dir=Path(args.output_dir),
        rows=args.rows,
        cols=args.cols,
        margin=args.margin,
        gutter=args.gutter,
    )
    for item in results:
        print(f"Saved {item.output_path} from box={item.crop_box}")


if __name__ == "__main__":
    main()
