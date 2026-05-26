from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests

from ltx_batch import batch as legacy_batch

from .common import deep_copy_jsonable, now_iso


SAVE_INPUT_NAMES = ("filename_prefix", "filename", "save_prefix")


def _optional_image_binding(workflow: dict[str, dict[str, Any]], hint: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return legacy_batch.detect_image_binding(workflow, hint)
    except Exception:
        return None


def _detect_image_bindings(workflow: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for node_id, node in workflow.items():
        class_type = str(node.get("class_type", ""))
        inputs = node.get("inputs", {})
        if "LoadImage" not in class_type or "image" not in inputs:
            continue
        title = legacy_batch.node_title(node)
        lowered = f"{title} {node_id} {class_type}".lower()
        role = "last_image" if any(token in lowered for token in ("last", "end", "final", "尾", "末")) else "first_image"
        candidates.append(
            {
                "id": str(node_id),
                "input_name": "image",
                "upload_input_name": "upload" if "upload" in inputs else "",
                "upload_value": "image",
                "role": role,
                "title": title,
                "class_type": class_type,
            }
        )

    if candidates and not any(item["role"] == "last_image" for item in candidates) and len(candidates) > 1:
        candidates[-1]["role"] = "last_image"
    return candidates


def _optional_seed_bindings(workflow: dict[str, dict[str, Any]], hints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        return legacy_batch.detect_seed_bindings(workflow, hints)
    except Exception:
        return []


def _node_sort_key(node_id: str) -> tuple[int, int, str]:
    prefix = node_id.split("__", 1)[0].split(":", 1)[0]
    if prefix.isdigit():
        return (0, int(prefix), node_id)
    return (1, 0, node_id)


def _save_input_name(node: dict[str, Any], requested: str = "") -> str:
    inputs = node.get("inputs", {})
    if requested and requested in inputs:
        return requested
    for name in SAVE_INPUT_NAMES:
        if name in inputs:
            return name
    return requested or "filename_prefix"


def _save_candidate_details(workflow: dict[str, dict[str, Any]]) -> list[str]:
    details: list[str] = []
    for node_id, node in workflow.items():
        class_type = str(node.get("class_type", ""))
        title = legacy_batch.node_title(node)
        names = legacy_batch.input_names(node)
        if any(name in names for name in SAVE_INPUT_NAMES) or "savevideo" in class_type.lower():
            details.append(f"{node_id}:{class_type}:{title}")
    return sorted(details, key=lambda item: _node_sort_key(item.split(":", 1)[0]))


def _detect_save_binding(workflow: dict[str, dict[str, Any]], hint: dict[str, Any]) -> dict[str, Any]:
    hint = dict(hint or {})
    hinted_id = str(hint.get("id", "")).strip()
    if hinted_id and not legacy_batch.is_placeholder(hinted_id):
        node = workflow.get(hinted_id)
        if not node:
            details = _save_candidate_details(workflow)
            raise ValueError(
                f"Save video node id '{hinted_id}' was not found in the workflow. "
                f"Available candidates: {details}"
            )
        hint["id"] = hinted_id
        hint["input_name"] = _save_input_name(node, str(hint.get("input_name", "")).strip())
        return hint

    candidates: list[tuple[str, dict[str, Any], int]] = []
    for node_id, node in workflow.items():
        class_type = str(node.get("class_type", ""))
        lowered_class = class_type.lower()
        title = legacy_batch.node_title(node).lower()
        names = legacy_batch.input_names(node)
        score = 0

        if class_type == "SaveVideo":
            score += 300
        elif "savevideo" in lowered_class:
            score += 260
        elif "save" in lowered_class and "video" in lowered_class:
            score += 180
        elif "save" in lowered_class or "保存" in title:
            score += 70

        if "filename_prefix" in names:
            score += 90
        elif any(name in names for name in ("filename", "save_prefix")):
            score += 60

        if "video" in names:
            score += 25
        if "__" not in str(node_id) and ":" not in str(node_id):
            score += 10

        if score > 0:
            candidates.append((str(node_id), node, score))

    exact_save_video = [
        item for item in candidates
        if str(item[1].get("class_type", "")) == "SaveVideo" and "filename_prefix" in item[1].get("inputs", {})
    ]
    if exact_save_video:
        chosen_id, chosen_node, _score = sorted(
            exact_save_video,
            key=lambda item: _node_sort_key(item[0]),
        )[0]
        return {"id": chosen_id, "input_name": _save_input_name(chosen_node)}

    try:
        detected = legacy_batch.detect_save_binding(workflow, hint)
        node = workflow.get(str(detected.get("id", "")), {})
        detected["input_name"] = _save_input_name(node, str(detected.get("input_name", "")).strip())
        return detected
    except Exception as exc:
        details = _save_candidate_details(workflow)
        raise ValueError(
            "Could not auto-detect a save video node. "
            "Fill only the SaveVideo node id in the workflow upload advanced field "
            "(for your current workflow this is usually 75). "
            f"Candidates: {details}"
        ) from exc


def _detect_negative_prompt_binding(
    workflow: dict[str, dict[str, Any]],
    hint: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    hint = dict(hint or {})
    if hint.get("id"):
        return hint

    candidates: list[tuple[str, dict[str, Any], int]] = []
    for node_id, node in workflow.items():
        class_type = str(node.get("class_type", ""))
        title = legacy_batch.node_title(node).lower()
        inputs = node.get("inputs", {})
        score = 0

        if class_type in legacy_batch.STRING_SOURCE_NODE_TYPES:
            score += 50
        if class_type == "CLIPTextEncode":
            score += 40
        if "negative" in title or "neg" in title:
            score += 120
        if "text" in inputs or "value" in inputs or "prompt" in inputs:
            score += 15

        text_value = str(inputs.get("text", "") or inputs.get("value", "") or inputs.get("prompt", "")).lower()
        if any(hint_value in text_value for hint_value in legacy_batch.NEGATIVE_HINTS):
            score += 80
        if score > 0:
            candidates.append((node_id, node, score))

    if not candidates:
        return None

    node_id, node = legacy_batch.choose_best_candidate(candidates, "negative prompt")
    current_input_name = str(hint.get("input_name", "")).strip()
    if not current_input_name or current_input_name not in node.get("inputs", {}):
        if "value" in node.get("inputs", {}) and str(node.get("class_type", "")) in legacy_batch.STRING_SOURCE_NODE_TYPES:
            current_input_name = "value"
        elif "text" in node.get("inputs", {}):
            current_input_name = "text"
        elif "prompt" in node.get("inputs", {}):
            current_input_name = "prompt"
        else:
            current_input_name = "value"
    return {"id": node_id, "input_name": current_input_name}


def _runtime_field_candidates(
    workflow: dict[str, dict[str, Any]],
    aliases: tuple[str, ...],
    title_aliases: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    detected: list[dict[str, Any]] = []
    alias_set = {alias.lower() for alias in aliases}
    title_alias_set = {alias.lower() for alias in title_aliases}
    for node_id, node in workflow.items():
        inputs = node.get("inputs", {})
        title_blob = f"{legacy_batch.node_title(node)} {node_id} {node.get('class_type', '')}".lower()
        for input_name, value in inputs.items():
            input_key = str(input_name).lower()
            input_matches = input_key in alias_set
            title_matches = bool(title_alias_set) and input_key == "value" and any(alias in title_blob for alias in title_alias_set)
            if not input_matches and not title_matches:
                continue
            if not isinstance(value, (int, float)):
                continue
            detected.append(
                {
                    "id": node_id,
                    "input_name": input_name,
                    "current_value": value,
                    "class_type": str(node.get("class_type", "")),
                    "title": legacy_batch.node_title(node),
                }
            )
    return detected


def _duration_options(default_value: Any) -> list[int]:
    values = {3, 5, 8, 10, 15, 20, 30}
    try:
        current = int(default_value)
        if current > 0:
            values.add(current)
    except (TypeError, ValueError):
        pass
    return sorted(values)


def inspect_workflow_profile(
    *,
    workflow_data: Any,
    base_url: str,
    config_hint: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    config_hint = config_hint or {}
    node_hint = config_hint.get("workflow_nodes", {})
    base_url = str(base_url or "").rstrip("/")
    with requests.Session() as session:
        compiled_template = legacy_batch.normalize_workflow_template(
            workflow_data,
            base_url=base_url,
            session=session,
        )

    positive_prompt = legacy_batch.detect_prompt_binding(
        compiled_template,
        dict(node_hint.get("positive_prompt", {})),
    )
    save_video = _detect_save_binding(
        compiled_template,
        dict(node_hint.get("save_video", {})),
    )
    primary_media = _optional_image_binding(
        compiled_template,
        dict(node_hint.get("image", {})),
    )
    media_inputs = _detect_image_bindings(compiled_template)
    negative_prompt = _detect_negative_prompt_binding(
        compiled_template,
        dict(node_hint.get("negative_prompt", {})),
    )
    seed_nodes = _optional_seed_bindings(
        compiled_template,
        list(node_hint.get("seed_nodes", [])),
    )
    width_bindings = _runtime_field_candidates(compiled_template, ("width", "resize_type.width"))
    height_bindings = _runtime_field_candidates(compiled_template, ("height", "resize_type.height"))
    duration_bindings = _runtime_field_candidates(
        compiled_template,
        ("duration", "duration_seconds", "seconds", "length", "video_length", "num_seconds"),
        ("duration", "time", "seconds", "时长", "秒"),
    )

    runtime_schema = [
        {
            "key": "save_prefix_root",
            "label": "ComfyUI Output Prefix Path",
            "type": "string",
            "default": str(config_hint.get("save_prefix_root", "batch_studio_v2")),
        },
        {
            "key": "output_name_prefix",
            "label": "Output File Prefix",
            "type": "string",
            "default": str(config_hint.get("output_name_prefix", "")),
        },
        {
            "key": "upload_subfolder",
            "label": "Upload Subfolder",
            "type": "string",
            "default": str(config_hint.get("upload_subfolder", "")),
        },
        {
            "key": "poll_interval_seconds",
            "label": "Poll Interval Seconds",
            "type": "number",
            "default": float(config_hint.get("poll_interval_seconds", 5)),
        },
        {
            "key": "timeout_seconds",
            "label": "Timeout Seconds",
            "type": "integer",
            "default": int(config_hint.get("timeout_seconds", 3600)),
        },
        {
            "key": "overwrite_outputs",
            "label": "Overwrite Outputs",
            "type": "boolean",
            "default": bool(config_hint.get("overwrite_outputs", False)),
        },
    ]
    if width_bindings:
        runtime_schema.append(
            {
                "key": "width_pixels",
                "label": "Output Width (px)",
                "type": "integer",
                "default": int(config_hint.get("width_pixels", width_bindings[0]["current_value"])),
            }
        )
    if height_bindings:
        runtime_schema.append(
            {
                "key": "height_pixels",
                "label": "Output Height (px)",
                "type": "integer",
                "default": int(config_hint.get("height_pixels", height_bindings[0]["current_value"])),
            }
        )
    if duration_bindings:
        duration_default = int(config_hint.get("duration_seconds", duration_bindings[0]["current_value"]))
        runtime_schema.append(
            {
                "key": "duration_seconds",
                "label": "Generation Duration (s)",
                "type": "integer",
                "default": duration_default,
                "options": _duration_options(duration_default),
            }
        )
    if seed_nodes:
        runtime_schema.append(
            {
                "key": "seed_base",
                "label": "Seed Base",
                "type": "integer",
                "default": int(config_hint.get("seed_base", 1)),
            }
        )
    if negative_prompt:
        runtime_schema.append(
            {
                "key": "negative_prompt_text",
                "label": "Negative Prompt Text",
                "type": "string",
                "default": str(config_hint.get("negative_prompt_text", "")),
            }
        )

    profile_manifest = {
        "name": "",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "input_contract": {
            "primary_media_kind": "image" if primary_media else "none",
            "supported_draft_kinds": (
                ["prompt_only", "storyboard_grid", "i2v_first_frame_batch", "i2v_first_last_batch"]
                if primary_media
                else ["prompt_only"]
            ),
        },
        "bindings": {
            "primary_media": deep_copy_jsonable(primary_media),
            "media_inputs": deep_copy_jsonable(media_inputs),
            "positive_prompt": deep_copy_jsonable(positive_prompt),
            "negative_prompt": deep_copy_jsonable(negative_prompt),
            "save_video": deep_copy_jsonable(save_video),
            "seed_nodes": deep_copy_jsonable(seed_nodes),
            "runtime_fields": {
                "width_pixels": deep_copy_jsonable(width_bindings),
                "height_pixels": deep_copy_jsonable(height_bindings),
                "duration_seconds": deep_copy_jsonable(duration_bindings),
            },
        },
        "runtime_schema": runtime_schema,
        "defaults": {
            "save_prefix_root": str(config_hint.get("save_prefix_root", "batch_studio_v2")),
            "output_name_prefix": str(config_hint.get("output_name_prefix", "")),
            "width_pixels": int(config_hint.get("width_pixels", width_bindings[0]["current_value"])) if width_bindings else None,
            "height_pixels": int(config_hint.get("height_pixels", height_bindings[0]["current_value"])) if height_bindings else None,
            "duration_seconds": int(config_hint.get("duration_seconds", duration_bindings[0]["current_value"])) if duration_bindings else None,
            "upload_subfolder": str(config_hint.get("upload_subfolder", "")),
            "poll_interval_seconds": float(config_hint.get("poll_interval_seconds", 5)),
            "timeout_seconds": int(config_hint.get("timeout_seconds", 3600)),
            "overwrite_outputs": bool(config_hint.get("overwrite_outputs", False)),
            "seed_base": int(config_hint.get("seed_base", 1)),
            "negative_prompt_text": str(config_hint.get("negative_prompt_text", "")),
        },
        "detection": {
            "base_url_used": base_url,
            "compiled_from_ui_workflow": isinstance(workflow_data, dict) and isinstance(workflow_data.get("nodes"), list),
            "compiled_node_count": len(compiled_template),
            "duration_options_seconds": _duration_options(duration_bindings[0]["current_value"]) if duration_bindings else [],
        },
    }
    return profile_manifest, deep_copy_jsonable(compiled_template)


def raw_workflow_from_text(text: str) -> Any:
    return json.loads(text)


def load_compiled_template(path: Path) -> dict[str, dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
