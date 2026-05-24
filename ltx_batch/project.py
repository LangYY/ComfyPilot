from __future__ import annotations

import json
import copy
from pathlib import Path
from typing import Any


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_path(root: Path, raw_path: str | Path) -> Path:
    path = Path(raw_path)
    return path if path.is_absolute() else (root / path)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def load_text(path: Path) -> str:
    with path.open("r", encoding="utf-8") as handle:
        return handle.read()


def save_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def default_config() -> dict[str, Any]:
    return {
        "comfyui_base_url": "http://127.0.0.1:8188",
        "workflow_path": "workflows/ltx_i2v_api.json",
        "prompts_path": "data/prompts.json",
        "cells_dir": "cells",
        "outputs_dir": "outputs",
        "failed_jobs_path": "failed_jobs.json",
        "comfyui_output_dir": "",
        "save_prefix_root": "video/ltx_storyboard_batch",
        "upload_images": True,
        "upload_subfolder": "",
        "poll_interval_seconds": 5,
        "timeout_seconds": 3600,
        "seed_base": 2026051800,
        "workflow_nodes": {
            "image": {
                "id": "",
                "input_name": "image",
                "upload_input_name": "upload",
                "upload_value": "image",
            },
            "positive_prompt": {
                "id": "",
                "input_name": "text",
            },
            "save_video": {
                "id": "",
                "input_name": "filename_prefix",
            },
            "seed_nodes": [],
        },
    }


def merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_dicts(result[key], value)
        elif isinstance(value, str) and value.strip() == "" and isinstance(result.get(key), str):
            continue
        else:
            result[key] = value
    return result


def normalize_config(config: dict[str, Any] | None) -> dict[str, Any]:
    config = config or {}
    return merge_dicts(default_config(), config)


def is_placeholder(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text.upper().startswith("REPLACE")
