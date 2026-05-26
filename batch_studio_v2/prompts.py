from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import re

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


def _normalize_prompt_value(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("Prompt text is empty.")
        return [text]
    return value


def _strip_loose_line_prefix(line: str) -> str:
    cleaned = re.sub(r"^\s*(?:[-*•]+|\d+[\.\)、)]|[（(]\d+[）)])\s*", "", line.strip())
    return cleaned.strip().strip('"').strip("'").strip()


def _loose_lines_to_prompts(text: str) -> list[str]:
    lines = [_strip_loose_line_prefix(line) for line in text.splitlines()]
    prompts = [line for line in lines if line]
    if prompts:
        return prompts
    cleaned = _strip_loose_line_prefix(text)
    if cleaned:
        return [cleaned]
    raise ValueError("Prompt text is empty.")


def normalize_prompt_payload_text(text: str) -> Any:
    raw = str(text or "").strip().lstrip("\ufeff")
    if not raw:
        raise ValueError("prompts text is required.")

    normalized = raw.translate(str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"}))
    candidates = [raw]
    if normalized != raw:
        candidates.append(normalized)

    for candidate in candidates:
        try:
            return _normalize_prompt_value(json.loads(candidate))
        except json.JSONDecodeError:
            pass

    trailing_comma_fixed = re.sub(r",\s*([}\]])", r"\1", normalized)
    if trailing_comma_fixed != normalized:
        try:
            return _normalize_prompt_value(json.loads(trailing_comma_fixed))
        except json.JSONDecodeError:
            pass

    if not normalized.startswith(("[", "{")):
        try:
            return _normalize_prompt_value(json.loads(f"[{normalized}]"))
        except json.JSONDecodeError:
            return _loose_lines_to_prompts(normalized)

    raise ValueError(
        "prompts text looks like JSON but could not be parsed. "
        "Use a JSON array/object, a single JSON string, or plain text prompts separated by new lines."
    )


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
