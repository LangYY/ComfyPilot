from __future__ import annotations

from pathlib import Path
from typing import Any

from ltx_batch.batch import build_output_name, load_prompts

from .common import make_id


def merge_run_settings(project_defaults: dict[str, Any], overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(project_defaults or {})
    for key, value in (overrides or {}).items():
        if value in (None, ""):
            continue
        merged[key] = value
    return merged


def compute_seed(entry: dict[str, Any], order: int, seed_base: int) -> int:
    if "seed" in entry and str(entry["seed"]).strip() != "":
        return int(entry["seed"])
    raw_index = entry.get("index")
    if raw_index not in (None, ""):
        return seed_base + int(raw_index)
    return seed_base + order


def input_ref(kind: str, relative_path: str, label: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "path": relative_path.replace("\\", "/"),
        "label": label,
    }


def task_from_prompt_entry(
    entry: dict[str, Any],
    *,
    order: int,
    seed_base: int,
    output_name_prefix: str = "",
    input_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prompt_text = str(entry.get("prompt") or entry.get("text") or "").strip()
    if not prompt_text:
        raise ValueError(f"Prompt entry #{order} is missing prompt text.")

    sidecar = {
        key: value
        for key, value in entry.items()
        if key not in {"prompt", "text"}
    }
    seed_value = compute_seed(entry, order, seed_base)
    expected_output_name = build_output_name(entry)
    if output_name_prefix.strip():
        name_path = Path(expected_output_name)
        expected_output_name = f"{output_name_prefix.strip()}{name_path.stem}{name_path.suffix}"
    return {
        "task_id": make_id("task"),
        "order": order,
        "source_index": int(entry.get("index", order)),
        "prompt_text": prompt_text,
        "sidecar": sidecar,
        "input_refs": list(input_refs or []),
        "runtime_overrides": {},
        "expected_output_name": expected_output_name,
        "seed_value": seed_value,
    }


def parse_prompt_payload(
    payload: Any,
    *,
    seed_base: int,
    output_name_prefix: str = "",
    input_refs_by_order: dict[int, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    entries = load_prompts(payload)
    tasks: list[dict[str, Any]] = []
    for order, entry in enumerate(entries, start=1):
        tasks.append(
            task_from_prompt_entry(
                entry,
                order=order,
                seed_base=seed_base,
                output_name_prefix=output_name_prefix,
                input_refs=(input_refs_by_order or {}).get(order),
            )
        )
    return tasks
