from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass
class StoryboardCell:
    index: int
    row: int
    col: int
    crop_box: tuple[int, int, int, int]
    output_path: Path


def validate_layout(image: Image.Image, rows: int, cols: int, margin: float, gutter: float) -> None:
    usable_width = image.width - (2 * margin) - ((cols - 1) * gutter)
    usable_height = image.height - (2 * margin) - ((rows - 1) * gutter)
    if rows <= 0 or cols <= 0:
        raise ValueError("rows and cols must both be greater than 0.")
    if usable_width <= 0 or usable_height <= 0:
        raise ValueError(
            "margin/gutter values leave no drawable area. Reduce them and try again."
        )


def compute_box(
    image_width: int,
    image_height: int,
    row: int,
    col: int,
    rows: int,
    cols: int,
    margin: float,
    gutter: float,
) -> tuple[int, int, int, int]:
    usable_width = image_width - (2 * margin) - ((cols - 1) * gutter)
    usable_height = image_height - (2 * margin) - ((rows - 1) * gutter)
    cell_width = usable_width / cols
    cell_height = usable_height / rows

    left = round(margin + col * (cell_width + gutter))
    top = round(margin + row * (cell_height + gutter))
    right = round(margin + (col + 1) * cell_width + col * gutter)
    bottom = round(margin + (row + 1) * cell_height + row * gutter)
    return left, top, right, bottom


def split_storyboard(
    storyboard_path: Path,
    output_dir: Path,
    rows: int = 4,
    cols: int = 3,
    margin: float = 0,
    gutter: float = 0,
) -> list[StoryboardCell]:
    storyboard_path = storyboard_path.resolve()
    output_dir = output_dir.resolve()

    if not storyboard_path.exists():
        raise FileNotFoundError(f"Storyboard image not found: {storyboard_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    cells: list[StoryboardCell] = []

    with Image.open(storyboard_path) as image:
        validate_layout(image, rows, cols, margin, gutter)

        index = 1
        for row in range(rows):
            for col in range(cols):
                crop_box = compute_box(
                    image.width,
                    image.height,
                    row,
                    col,
                    rows,
                    cols,
                    margin,
                    gutter,
                )
                output_path = output_dir / f"{index:02d}.png"
                image.crop(crop_box).save(output_path)
                cells.append(
                    StoryboardCell(
                        index=index,
                        row=row,
                        col=col,
                        crop_box=crop_box,
                        output_path=output_path,
                    )
                )
                index += 1

    return cells

