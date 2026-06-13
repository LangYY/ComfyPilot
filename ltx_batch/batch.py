from __future__ import annotations

import copy
import json
import mimetypes
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from .project import is_placeholder, load_json, normalize_config, project_root, resolve_path, save_json


VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".gif"}
NEGATIVE_HINTS = ("negative", "blurry", "watermark", "subtitle", "low quality", "bad")
STRING_SOURCE_NODE_TYPES = {"PrimitiveString", "PrimitiveStringMultiline", "StringConstant"}
WIDGET_VALUE_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "AUDIO_UI", "COMBO"}
SKIP_UI_NODE_TYPES = {"Reroute", "Note", "MarkdownNote"}

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[dict[str, Any]], None]
CancelCallback = Callable[[], bool]


class BatchCancelled(RuntimeError):
    pass


@dataclass
class BatchRunOptions:
    config_path: Path
    start_index: int = 1
    end_index: int | None = None
    overwrite: bool = False
    poll_interval: float | None = None
    timeout_seconds: int | None = None


@dataclass
class BatchSummary:
    total: int
    completed: int
    failed: int
    skipped: int
    cancelled: int
    failures: list[dict[str, Any]]
    bindings: dict[str, Any]
    outputs: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def emit_log(callback: LogCallback | None, message: str) -> None:
    if callback:
        callback(message)


def emit_progress(callback: ProgressCallback | None, payload: dict[str, Any]) -> None:
    if callback:
        callback(payload)


def is_cancelled(callback: CancelCallback | None) -> bool:
    try:
        return bool(callback and callback())
    except Exception:
        return False


def normalize_graph_links(raw_links: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    if not isinstance(raw_links, list):
        return normalized

    for item in raw_links:
        if isinstance(item, dict):
            normalized.append(
                {
                    "id": item.get("id"),
                    "origin_id": item.get("origin_id"),
                    "origin_slot": item.get("origin_slot", 0),
                    "target_id": item.get("target_id"),
                    "target_slot": item.get("target_slot", 0),
                    "type": item.get("type"),
                }
            )
        elif isinstance(item, list) and len(item) >= 5:
            normalized.append(
                {
                    "id": item[0],
                    "origin_id": item[1],
                    "origin_slot": item[2],
                    "target_id": item[3],
                    "target_slot": item[4],
                    "type": item[5] if len(item) > 5 else None,
                }
            )
    return normalized


def load_subgraph_definitions(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    definitions = data.get("definitions", {})
    subgraphs = definitions.get("subgraphs", [])
    if not isinstance(subgraphs, list):
        return {}

    loaded: dict[str, dict[str, Any]] = {}
    for item in subgraphs:
        if not isinstance(item, dict):
            continue
        subgraph_id = str(item.get("id", "")).strip()
        if subgraph_id:
            loaded[subgraph_id] = item
    return loaded


def flat_node_id(prefix: str, node_id: Any) -> str:
    return f"{prefix}{node_id}"


def resolve_origin_refs(
    origin_id: Any,
    origin_slot: int,
    group_slots: dict[str, dict[str, Any]],
    prefix: str,
) -> list[tuple[str, int]]:
    if origin_id is None:
        return []
    key = str(origin_id)
    if key in group_slots:
        output_ref = group_slots[key]["outputs"].get(origin_slot)
        return [output_ref] if output_ref else []
    if str(origin_id).startswith("-"):
        return []
    return [(flat_node_id(prefix, origin_id), int(origin_slot))]


def resolve_target_refs(
    target_id: Any,
    target_slot: int,
    group_slots: dict[str, dict[str, Any]],
    prefix: str,
) -> list[tuple[str, int]]:
    if target_id is None:
        return []
    key = str(target_id)
    if key in group_slots:
        return list(group_slots[key]["inputs"].get(target_slot, []))
    if str(target_id).startswith("-"):
        return []
    return [(flat_node_id(prefix, target_id), int(target_slot))]


def flatten_subgraph_instance(
    node: dict[str, Any],
    subgraph: dict[str, Any],
    definition_map: dict[str, dict[str, Any]],
    prefix: str,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    sub_prefix = f"{prefix}{node['id']}__"
    flat_nodes, flat_links, nested_group_slots = flatten_workflow_nodes(
        subgraph.get("nodes", []),
        subgraph.get("links", []),
        definition_map,
        sub_prefix,
    )
    link_map = {
        item["id"]: item for item in normalize_graph_links(subgraph.get("links", [])) if item.get("id") is not None
    }

    input_mappings: dict[int, list[tuple[str, int]]] = {}
    for slot_index, input_slot in enumerate(subgraph.get("inputs", [])):
        targets: list[tuple[str, int]] = []
        for link_id in input_slot.get("linkIds", []) or []:
            link = link_map.get(link_id)
            if not link:
                continue
            targets.extend(
                resolve_target_refs(
                    link["target_id"],
                    int(link.get("target_slot", 0)),
                    nested_group_slots,
                    sub_prefix,
                )
            )
        input_mappings[slot_index] = targets

    output_mappings: dict[int, tuple[str, int]] = {}
    for slot_index, output_slot in enumerate(subgraph.get("outputs", [])):
        refs: list[tuple[str, int]] = []
        for link_id in output_slot.get("linkIds", []) or []:
            link = link_map.get(link_id)
            if not link:
                continue
            refs.extend(
                resolve_origin_refs(
                    link["origin_id"],
                    int(link.get("origin_slot", 0)),
                    nested_group_slots,
                    sub_prefix,
                )
            )
        if refs:
            output_mappings[slot_index] = refs[0]

    return flat_nodes, flat_links, {"inputs": input_mappings, "outputs": output_mappings}


def flatten_workflow_nodes(
    nodes: Any,
    links: Any,
    definition_map: dict[str, dict[str, Any]],
    prefix: str = "",
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    flat_nodes: dict[str, dict[str, Any]] = {}
    flat_links: list[dict[str, Any]] = []
    group_slots: dict[str, dict[str, Any]] = {}

    if not isinstance(nodes, list):
        return flat_nodes, flat_links, group_slots

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type", ""))
        if node_type in definition_map:
            sub_nodes, sub_links, io_map = flatten_subgraph_instance(
                node=node,
                subgraph=definition_map[node_type],
                definition_map=definition_map,
                prefix=prefix,
            )
            flat_nodes.update(sub_nodes)
            flat_links.extend(sub_links)
            group_slots[str(node["id"])] = io_map
        else:
            flat_nodes[flat_node_id(prefix, node["id"])] = copy.deepcopy(node)

    for link in normalize_graph_links(links):
        origin_id = link.get("origin_id")
        target_id = link.get("target_id")
        if str(origin_id).startswith("-") or str(target_id).startswith("-"):
            continue

        origin_refs = resolve_origin_refs(
            origin_id,
            int(link.get("origin_slot", 0)),
            group_slots,
            prefix,
        )
        target_refs = resolve_target_refs(
            target_id,
            int(link.get("target_slot", 0)),
            group_slots,
            prefix,
        )
        for origin_ref in origin_refs:
            for target_ref in target_refs:
                flat_links.append(
                    {
                        "origin_id": origin_ref[0],
                        "origin_slot": origin_ref[1],
                        "target_id": target_ref[0],
                        "target_slot": target_ref[1],
                        "type": link.get("type"),
                    }
                )

    return flat_nodes, flat_links, group_slots


def fetch_object_info(session: requests.Session, base_url: str) -> dict[str, Any]:
    try:
        response = session.get(f"{base_url}/object_info", timeout=120)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            "ComfyUI must be running and reachable so this project can compile subgraph-based API workflows. "
            f"Tried {base_url}/object_info and got: {exc}"
        ) from exc
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("ComfyUI /object_info did not return a JSON object.")
    return payload


def ordered_input_names(node_info: dict[str, Any]) -> list[str]:
    names: list[str] = []
    input_order = node_info.get("input_order", {})
    input_groups = node_info.get("input", {})
    for section in ("required", "optional"):
        section_names = input_order.get(section)
        if isinstance(section_names, list):
            names.extend(section_names)
        elif isinstance(input_groups.get(section), dict):
            names.extend(input_groups[section].keys())
    return names


def input_spec_for_name(node_info: dict[str, Any], input_name: str) -> Any:
    input_groups = node_info.get("input", {})
    for section in ("required", "optional"):
        section_inputs = input_groups.get(section, {})
        if input_name in section_inputs:
            return section_inputs[input_name]
    return None


def input_uses_widget(type_spec: Any, input_entry: dict[str, Any] | None) -> bool:
    if input_entry and "widget" in input_entry:
        return True
    if not type_spec:
        return False
    raw_type = type_spec[0] if isinstance(type_spec, (list, tuple)) and type_spec else type_spec
    if isinstance(raw_type, list):
        return True
    type_name = str(raw_type)
    return type_name in WIDGET_VALUE_TYPES or type_name.endswith("UPLOAD") or type_name.endswith("_UI")


def raw_type_name(type_spec: Any) -> str:
    if not type_spec:
        return ""
    raw_type = type_spec[0] if isinstance(type_spec, (list, tuple)) and type_spec else type_spec
    if isinstance(raw_type, list):
        return "COMBO"
    return str(raw_type)


def compile_ui_workflow_to_prompt(
    data: dict[str, Any],
    object_info: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    definition_map = load_subgraph_definitions(data)
    flat_nodes, flat_links, _group_slots = flatten_workflow_nodes(
        data.get("nodes", []),
        data.get("links", []),
        definition_map,
    )

    incoming_links_by_target: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for link in flat_links:
        key = (str(link["target_id"]), int(link.get("target_slot", 0)))
        incoming_links_by_target.setdefault(key, []).append(link)

    def resolve_origin_reference(
        link: dict[str, Any],
        seen: set[tuple[str, int]] | None = None,
    ) -> tuple[str, int] | None:
        origin_id = str(link["origin_id"])
        origin_slot = int(link.get("origin_slot", 0))
        origin_node = flat_nodes.get(origin_id)
        if not origin_node:
            return origin_id, origin_slot

        origin_type = str(origin_node.get("type", ""))
        if origin_type == "Reroute":
            marker = (origin_id, origin_slot)
            seen = seen or set()
            if marker in seen:
                return None
            seen.add(marker)
            upstream_links = incoming_links_by_target.get((origin_id, 0), [])
            for upstream in upstream_links:
                resolved = resolve_origin_reference(upstream, seen)
                if resolved is not None:
                    return resolved
            return None

        return origin_id, origin_slot

    link_targets: dict[tuple[str, str], tuple[str, int]] = {}
    for link in flat_links:
        target_node = flat_nodes.get(str(link["target_id"]))
        if not target_node:
            continue
        if str(target_node.get("type", "")) in SKIP_UI_NODE_TYPES:
            continue
        target_inputs = target_node.get("inputs", []) or []
        target_slot = int(link.get("target_slot", 0))
        if target_slot >= len(target_inputs):
            continue
        input_name = str(target_inputs[target_slot].get("name", "")).strip()
        if input_name:
            resolved_origin = resolve_origin_reference(link)
            if resolved_origin is not None:
                link_targets[(str(link["target_id"]), input_name)] = resolved_origin

    prompt: dict[str, dict[str, Any]] = {}
    for node_id, node in flat_nodes.items():
        node_type = str(node.get("type", "")).strip()
        if not node_type:
            continue
        if node_type in SKIP_UI_NODE_TYPES:
            continue

        node_info = object_info.get(node_type)
        if not isinstance(node_info, dict):
            outputs = node.get("outputs", []) or []
            if outputs:
                raise ValueError(
                    f"ComfyUI /object_info did not return metadata for node type '{node_type}'. "
                    "Make sure the workflow's custom nodes are installed and loaded."
                )
            continue

        compiled_inputs: dict[str, Any] = {}
        input_entries = {
            str(entry.get("name", "")).strip(): entry
            for entry in (node.get("inputs", []) or [])
            if isinstance(entry, dict) and str(entry.get("name", "")).strip()
        }
        widget_values = list(node.get("widgets_values") or [])
        widget_index = 0
        processed_names: set[str] = set()

        for input_name in ordered_input_names(node_info):
            type_spec = input_spec_for_name(node_info, input_name)
            input_entry = input_entries.get(input_name)
            linked_value = link_targets.get((node_id, input_name))
            type_name = raw_type_name(type_spec)
            has_widget = input_uses_widget(type_spec, input_entry)

            if type_name == "COMFY_DYNAMICCOMBO_V3":
                selected_key: Any = None
                if widget_index < len(widget_values):
                    selected_key = copy.deepcopy(widget_values[widget_index])
                    widget_index += 1
                if selected_key is not None:
                    compiled_inputs[input_name] = selected_key
                    processed_names.add(input_name)

                option_specs = type_spec[1].get("options", []) if isinstance(type_spec, (list, tuple)) and len(type_spec) > 1 and isinstance(type_spec[1], dict) else []
                selected_option = next(
                    (
                        option for option in option_specs
                        if isinstance(option, dict) and str(option.get("key")) == str(selected_key)
                    ),
                    None,
                )
                nested_required = (
                    selected_option.get("inputs", {}).get("required", {})
                    if isinstance(selected_option, dict)
                    else {}
                )
                for nested_name in nested_required.keys():
                    full_name = f"{input_name}.{nested_name}"
                    nested_link = link_targets.get((node_id, full_name))
                    nested_value: Any = None
                    if widget_index < len(widget_values):
                        nested_value = copy.deepcopy(widget_values[widget_index])
                        widget_index += 1
                    if nested_link is not None:
                        compiled_inputs[full_name] = [nested_link[0], nested_link[1]]
                    elif nested_value is not None:
                        compiled_inputs[full_name] = nested_value
                    processed_names.add(full_name)
                continue

            widget_value: Any = None
            if has_widget and widget_index < len(widget_values):
                widget_value = copy.deepcopy(widget_values[widget_index])
                widget_index += 1

            if linked_value is not None:
                compiled_inputs[input_name] = [linked_value[0], linked_value[1]]
            elif has_widget and widget_value is not None:
                compiled_inputs[input_name] = widget_value
            if input_name in compiled_inputs:
                processed_names.add(input_name)

        for input_name, input_entry in input_entries.items():
            if input_name in processed_names or input_name in compiled_inputs:
                continue
            linked_value = link_targets.get((node_id, input_name))
            if linked_value is not None:
                compiled_inputs[input_name] = [linked_value[0], linked_value[1]]
                processed_names.add(input_name)
                continue

            if input_uses_widget(None, input_entry) and widget_index < len(widget_values):
                compiled_inputs[input_name] = copy.deepcopy(widget_values[widget_index])
                widget_index += 1
                processed_names.add(input_name)

        prompt[node_id] = {
            "class_type": node_type,
            "inputs": compiled_inputs,
            "_meta": {
                "title": str(node.get("title") or node.get("properties", {}).get("Node name for S&R", "")).strip()
            },
        }

    return prompt


def normalize_workflow_template(
    data: Any,
    *,
    base_url: str | None = None,
    session: requests.Session | None = None,
) -> dict[str, dict[str, Any]]:
    if isinstance(data, dict):
        api_nodes = {
            str(node_id): node
            for node_id, node in data.items()
            if isinstance(node, dict) and "class_type" in node
        }
        if api_nodes:
            return api_nodes

        prompt_dict = data.get("prompt")
        if isinstance(prompt_dict, dict):
            return normalize_workflow_template(prompt_dict, base_url=base_url, session=session)

        if isinstance(data.get("nodes"), list):
            if not base_url or session is None:
                raise ValueError(
                    "This workflow is in UI/subgraph format. Set comfyui_base_url and keep ComfyUI running so "
                    "the project can fetch /object_info and compile it into API prompt format."
                )
            object_info = fetch_object_info(session, base_url)
            return compile_ui_workflow_to_prompt(data, object_info)

        extra_prompt = data.get("extra", {}).get("prompt")
        if isinstance(extra_prompt, dict):
            return normalize_workflow_template(extra_prompt, base_url=base_url, session=session)

    raise ValueError(
        "Workflow JSON is neither ComfyUI API prompt format nor a supported UI workflow export."
    )


def _prompt_key_rank(key: Any) -> tuple[int, int, int] | None:
    normalized = re.sub(r"[\s_-]+", "", str(key or "").strip().lower())
    aliases = {
        "prompt": 0,
        "text": 1,
        "visualprompt": 2,
        "videoprompt": 3,
        "positiveprompt": 4,
    }
    for alias, rank in aliases.items():
        if normalized == alias:
            return (0, rank, 0)
        match = re.fullmatch(rf"{alias}(\d+)", normalized)
        if match:
            return (1, rank, int(match.group(1)))
    return None


def _normalized_prompt_key(key: Any) -> str:
    return re.sub(r"[\s_-]+", "", str(key or "").strip().lower())


def _is_prompt_metadata_key(key: Any) -> bool:
    return _normalized_prompt_key(key) in {
        "index",
        "id",
        "title",
        "name",
        "subtitle",
        "outputname",
        "seed",
        "duration",
        "durationseconds",
        "zodiac",
        "ballname",
        "floortheme",
        "sfx",
        "sfxstyle",
        "bgm",
        "bgmmood",
    }


def _freeform_prompt_candidates(item: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        (str(key), value.strip())
        for key, value in item.items()
        if _prompt_key_rank(key) is None
        and not _is_prompt_metadata_key(key)
        and isinstance(value, str)
        and value.strip()
    ]


def _extract_prompt_text(item: dict[str, Any], position: int) -> tuple[str, str]:
    candidates: list[tuple[tuple[int, int, int], str, str]] = []
    for key, value in item.items():
        rank = _prompt_key_rank(key)
        text = str(value or "").strip()
        if rank is not None and text:
            candidates.append((rank, str(key), text))

    if not candidates:
        freeform_candidates = _freeform_prompt_candidates(item)
        if len(freeform_candidates) == 1:
            return freeform_candidates[0][1], freeform_candidates[0][0]
        if len(freeform_candidates) > 1:
            keys = ", ".join(candidate[0] for candidate in freeform_candidates)
            raise ValueError(f"Prompt entry #{position} has ambiguous text fields: {keys}.")
        raise ValueError(
            f"Prompt entry #{position} is missing prompt text. "
            "Use prompt/text, a common variant, or one clearly identifiable custom text field."
        )

    candidates.sort(key=lambda candidate: candidate[0])
    exact_candidates = [candidate for candidate in candidates if candidate[0][0] == 0]
    if exact_candidates:
        candidates = exact_candidates
        candidates.sort(key=lambda candidate: candidate[0])
    elif len(candidates) > 1:
        keys = ", ".join(candidate[1] for candidate in candidates)
        raise ValueError(f"Prompt entry #{position} has ambiguous prompt fields: {keys}.")
    _rank, source_key, prompt_text = candidates[0]
    return prompt_text, source_key


def _looks_like_prompt_object(data: Any) -> bool:
    return isinstance(data, dict) and (
        any(_prompt_key_rank(key) is not None for key in data)
        or bool(_freeform_prompt_candidates(data))
    )


def load_prompts(data: Any) -> list[dict[str, Any]]:
    prompts: list[Any]
    if isinstance(data, list):
        prompts = data
    elif _looks_like_prompt_object(data):
        prompts = [data]
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        prompts = data["items"]
    elif isinstance(data, dict) and isinstance(data.get("scenes"), list):
        master_prompt = str(data.get("master_prompt", "")).strip()
        prompts = []
        for scene in data["scenes"]:
            if isinstance(scene, str):
                item = {"prompt": scene}
            elif isinstance(scene, dict):
                item = dict(scene)
            else:
                raise ValueError("Each scene entry in prompts.json must be a string or object.")

            if _looks_like_prompt_object(item):
                visual_prompt, source_key = _extract_prompt_text(item, len(prompts) + 1)
            else:
                visual_prompt, source_key = "", ""
            if master_prompt and visual_prompt:
                item["prompt"] = f"{master_prompt}\n\nScene prompt:\n{visual_prompt}"
            else:
                item["prompt"] = visual_prompt or master_prompt
            if source_key and source_key != "prompt":
                item.pop(source_key, None)
            item["output_name"] = item.get("output_name") or ""
            prompts.append(item)
    else:
        raise ValueError(
            "prompts.json must be a list, an object with an items list, or an object with a scenes list."
        )

    normalized: list[dict[str, Any]] = []
    for position, entry in enumerate(prompts, start=1):
        if isinstance(entry, str):
            item = {"prompt": entry}
        elif isinstance(entry, dict):
            item = dict(entry)
        else:
            raise ValueError("Each prompt entry in prompts.json must be a string or object.")

        prompt_text, source_key = _extract_prompt_text(item, position)

        raw_index = item.get("index")
        item["index"] = position if raw_index in (None, "") else int(raw_index)
        item["prompt"] = prompt_text
        if source_key != "prompt":
            item.pop(source_key, None)
        normalized.append(item)
    return sorted(normalized, key=lambda item: item["index"])


def filter_prompts(
    prompts: list[dict[str, Any]],
    start_index: int,
    end_index: int | None,
) -> list[dict[str, Any]]:
    if end_index is None:
        end_index = max(item["index"] for item in prompts)
    return [item for item in prompts if start_index <= item["index"] <= end_index]


def node_title(node: dict[str, Any]) -> str:
    return str(node.get("_meta", {}).get("title", ""))


def input_names(node: dict[str, Any]) -> set[str]:
    return set(node.get("inputs", {}).keys())


def choose_best_candidate(
    candidates: list[tuple[str, dict[str, Any], int]],
    label: str,
) -> tuple[str, dict[str, Any]]:
    if not candidates:
        raise ValueError(f"Could not auto-detect a {label} node in the workflow.")

    candidates = sorted(candidates, key=lambda item: item[2], reverse=True)
    if len(candidates) == 1 or candidates[0][2] > candidates[1][2]:
        return candidates[0][0], candidates[0][1]

    details = [
        f"{node_id}:{node.get('class_type')}:{node_title(node)}"
        for node_id, node, _score in candidates
    ]
    raise ValueError(
        f"Could not auto-detect a unique {label} node. Fill the node id in config. "
        f"Candidates: {details}"
    )


def detect_image_binding(
    workflow: dict[str, dict[str, Any]],
    binding: dict[str, Any],
) -> dict[str, Any]:
    if not is_placeholder(binding.get("id")):
        return binding

    candidates: list[tuple[str, dict[str, Any], int]] = []
    for node_id, node in workflow.items():
        class_type = str(node.get("class_type", ""))
        names = input_names(node)
        score = 0
        if class_type == "LoadImage":
            score += 100
        if "LoadImage" in class_type:
            score += 50
        if "image" in names:
            score += 25
        if score:
            candidates.append((node_id, node, score))

    if not candidates:
        raise ValueError(
            "Could not auto-detect a LoadImage node in the workflow. "
            "Fill workflow_nodes.image.id manually, or make sure the workflow exposes a regular "
            "LoadImage first-frame source after compilation."
        )

    node_id, _node = choose_best_candidate(candidates, "LoadImage")
    resolved = dict(binding)
    resolved["id"] = node_id
    if is_placeholder(resolved.get("input_name")):
        resolved["input_name"] = "image"
    if is_placeholder(resolved.get("upload_input_name")):
        resolved["upload_input_name"] = "upload"
    if is_placeholder(resolved.get("upload_value")):
        resolved["upload_value"] = "image"
    return resolved


def detect_prompt_binding(
    workflow: dict[str, dict[str, Any]],
    binding: dict[str, Any],
) -> dict[str, Any]:
    if not is_placeholder(binding.get("id")):
        return binding

    candidates: list[tuple[str, dict[str, Any], int]] = []
    for node_id, node in workflow.items():
        class_type = str(node.get("class_type", ""))
        title = node_title(node).lower()
        inputs = node.get("inputs", {})
        score = 0

        if class_type in STRING_SOURCE_NODE_TYPES:
            score += 80
        if "value" in inputs and isinstance(inputs.get("value"), str):
            score += 35
        if class_type == "CLIPTextEncode":
            score += 50
        if "text" in inputs:
            score += 25
        if "prompt" in title:
            score += 90
        if "positive" in title:
            score += 100
        prompt_value = str(inputs.get("value", "")).lower()
        text_value = str(inputs.get("text", "")).lower()
        combined_value = text_value or prompt_value
        if combined_value.strip() == "":
            score += 60
        if any(hint in combined_value or hint in title for hint in NEGATIVE_HINTS):
            score -= 100
        if score > 0:
            candidates.append((node_id, node, score))

    node_id, node = choose_best_candidate(candidates, "positive prompt")
    resolved = dict(binding)
    resolved["id"] = node_id
    current_input_name = str(resolved.get("input_name", "")).strip()
    if is_placeholder(current_input_name) or current_input_name not in node.get("inputs", {}):
        if "value" in node.get("inputs", {}) and str(node.get("class_type", "")) in STRING_SOURCE_NODE_TYPES:
            resolved["input_name"] = "value"
        elif "text" in node.get("inputs", {}):
            resolved["input_name"] = "text"
        elif "prompt" in node.get("inputs", {}):
            resolved["input_name"] = "prompt"
        else:
            resolved["input_name"] = "value"
    return resolved


def detect_save_binding(
    workflow: dict[str, dict[str, Any]],
    binding: dict[str, Any],
) -> dict[str, Any]:
    if not is_placeholder(binding.get("id")):
        return binding

    candidates: list[tuple[str, dict[str, Any], int]] = []
    for node_id, node in workflow.items():
        class_type = str(node.get("class_type", ""))
        title = node_title(node).lower()
        names = input_names(node)
        score = 0
        if any(name in names for name in ("filename_prefix", "filename", "save_prefix")):
            score += 50
        if "video" in class_type.lower() or "video" in title:
            score += 50
        if "save" in class_type.lower():
            score += 25
        if score > 0:
            candidates.append((node_id, node, score))

    node_id, node = choose_best_candidate(candidates, "save video")
    resolved = dict(binding)
    resolved["id"] = node_id
    if is_placeholder(resolved.get("input_name")):
        for name in ("filename_prefix", "filename", "save_prefix"):
            if name in node.get("inputs", {}):
                resolved["input_name"] = name
                break
    return resolved


def detect_seed_bindings(
    workflow: dict[str, dict[str, Any]],
    bindings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    usable = [binding for binding in bindings if not is_placeholder(binding.get("id"))]
    if usable:
        return usable

    detected: list[dict[str, Any]] = []
    for node_id, node in workflow.items():
        names = input_names(node)
        if "noise_seed" in names:
            detected.append({"id": node_id, "input_name": "noise_seed"})
        elif "seed" in names:
            detected.append({"id": node_id, "input_name": "seed"})

    if not detected:
        raise ValueError(
            "Could not auto-detect any seed nodes. Add workflow_nodes.seed_nodes to config."
        )
    return detected


def resolve_bindings(
    workflow: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    node_config = config.get("workflow_nodes", {})
    image = detect_image_binding(workflow, dict(node_config.get("image", {})))
    positive_prompt = detect_prompt_binding(
        workflow, dict(node_config.get("positive_prompt", {}))
    )
    save_video = detect_save_binding(workflow, dict(node_config.get("save_video", {})))
    seed_nodes = detect_seed_bindings(workflow, list(node_config.get("seed_nodes", [])))

    return {
        "image": image,
        "positive_prompt": positive_prompt,
        "save_video": save_video,
        "seed_nodes": seed_nodes,
    }


def ensure_node(workflow: dict[str, dict[str, Any]], binding: dict[str, Any], label: str) -> dict[str, Any]:
    node_id = str(binding["id"])
    if node_id not in workflow:
        raise KeyError(f"{label} node id {node_id} was not found in the workflow.")
    return workflow[node_id]


def upload_image(
    session: requests.Session,
    base_url: str,
    image_path: Path,
    upload_subfolder: str = "",
) -> dict[str, Any]:
    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
    with image_path.open("rb") as handle:
        response = session.post(
            f"{base_url}/upload/image",
            data={
                "type": "input",
                "overwrite": "true",
                "subfolder": upload_subfolder,
            },
            files={"image": (image_path.name, handle, mime_type)},
            timeout=120,
        )
    response.raise_for_status()
    return response.json()


def uploaded_image_value(upload_response: dict[str, Any]) -> str:
    subfolder = str(upload_response.get("subfolder", "")).strip("/\\")
    name = str(upload_response["name"])
    return f"{subfolder}/{name}" if subfolder else name


def build_output_name(item: dict[str, Any]) -> str:
    explicit_name = str(item.get("output_name", "")).strip()
    if explicit_name:
        return explicit_name
    index = int(item["index"])
    zodiac = str(item.get("zodiac", "")).strip() or f"scene_{index:02d}"
    subject_name = (
        str(item.get("ball_name", "")).strip()
        or str(item.get("floor_theme", "")).strip()
        or str(item.get("subtitle", "")).strip()
        or "clip"
    )
    return f"{index:02d}_{zodiac}_{subject_name}.mp4"


def sanitize_prefix(name: str) -> str:
    stem = Path(name).stem
    cleaned = re.sub(r'[<>:"\\\\|?*]', "_", stem)
    cleaned = cleaned.replace(" ", "_")
    return cleaned


def apply_batch_values(
    workflow: dict[str, dict[str, Any]],
    bindings: dict[str, Any],
    image_value: str,
    prompt_text: str,
    seed_value: int,
    filename_prefix: str,
) -> None:
    image_binding = bindings["image"]
    image_node = ensure_node(workflow, image_binding, "image")
    image_node.setdefault("inputs", {})[image_binding.get("input_name", "image")] = image_value
    upload_input_name = image_binding.get("upload_input_name")
    if upload_input_name:
        image_node["inputs"][upload_input_name] = image_binding.get("upload_value", "image")

    prompt_binding = bindings["positive_prompt"]
    prompt_node = ensure_node(workflow, prompt_binding, "positive prompt")
    prompt_node.setdefault("inputs", {})[prompt_binding["input_name"]] = prompt_text

    save_binding = bindings["save_video"]
    save_node = ensure_node(workflow, save_binding, "save video")
    save_node.setdefault("inputs", {})[save_binding["input_name"]] = filename_prefix

    for seed_binding in bindings["seed_nodes"]:
        seed_node = ensure_node(workflow, seed_binding, "seed")
        seed_node.setdefault("inputs", {})[seed_binding["input_name"]] = seed_value


def submit_prompt(
    session: requests.Session,
    base_url: str,
    prompt: dict[str, dict[str, Any]],
) -> str:
    payload = {"prompt": prompt, "client_id": str(uuid.uuid4())}
    response = session.post(f"{base_url}/prompt", json=payload, timeout=120)
    if response.status_code >= 400:
        raise RuntimeError(
            f"ComfyUI rejected the workflow ({response.status_code}): {response.text}"
        )
    data = response.json()
    prompt_id = data.get("prompt_id")
    if not prompt_id:
        raise RuntimeError(f"ComfyUI did not return prompt_id: {data}")
    return str(prompt_id)


def extract_history_record(history_payload: Any, prompt_id: str) -> dict[str, Any] | None:
    if isinstance(history_payload, dict):
        if prompt_id in history_payload and isinstance(history_payload[prompt_id], dict):
            return history_payload[prompt_id]
        if "outputs" in history_payload or "status" in history_payload:
            return history_payload
    return None


def summarize_status(record: dict[str, Any]) -> str:
    status = record.get("status", {})
    status_str = status.get("status_str") or "unknown"
    messages = status.get("messages") or []
    if not messages:
        return str(status_str)
    last_message = messages[-1]
    return f"{status_str}: {last_message}"


def wait_for_completion(
    session: requests.Session,
    base_url: str,
    prompt_id: str,
    poll_interval_seconds: float,
    timeout_seconds: int,
    is_cancelled_callback: CancelCallback | None = None,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    history_url = f"{base_url}/history/{prompt_id}"
    queue_url = f"{base_url}/queue"

    while time.time() < deadline:
        if is_cancelled(is_cancelled_callback):
            raise BatchCancelled(f"Batch stop requested while waiting for prompt_id={prompt_id}")

        history_response = session.get(history_url, timeout=60)
        history_response.raise_for_status()
        record = extract_history_record(history_response.json(), prompt_id)
        if record:
            status = record.get("status", {})
            status_str = str(status.get("status_str", "")).lower()
            if status.get("completed") or record.get("outputs"):
                if status_str in {"error", "failed"}:
                    raise RuntimeError(summarize_status(record))
                return record
            if status_str in {"error", "failed"}:
                raise RuntimeError(summarize_status(record))

        queue_response = session.get(queue_url, timeout=60)
        queue_response.raise_for_status()
        queue_info = queue_response.json()
        still_known = json.dumps(queue_info, ensure_ascii=False)
        if prompt_id not in still_known and record and record.get("outputs"):
            return record
        if prompt_id not in still_known and not record:
            raise RuntimeError(
                f"Prompt is no longer present in ComfyUI queue/history: {prompt_id}. "
                "If ComfyUI restarted, rerun the batch to submit a fresh prompt."
            )

        if is_cancelled(is_cancelled_callback):
            raise BatchCancelled(f"Batch stop requested while waiting for prompt_id={prompt_id}")
        time.sleep(poll_interval_seconds)

    raise TimeoutError(
        f"Timed out after {timeout_seconds} seconds while waiting for prompt_id={prompt_id}"
    )


def collect_file_entries(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if "filename" in value and "type" in value:
            found.append(value)
        for nested in value.values():
            found.extend(collect_file_entries(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(collect_file_entries(nested))
    return found


def pick_video_entry(record: dict[str, Any]) -> dict[str, Any]:
    entries = collect_file_entries(record.get("outputs", {}))
    for entry in entries:
        if Path(str(entry.get("filename", ""))).suffix.lower() in VIDEO_EXTENSIONS:
            return entry
    raise RuntimeError("The completed ComfyUI history did not contain any video file outputs.")


def copy_video_to_project(
    record: dict[str, Any],
    comfyui_output_dir: Path,
    outputs_dir: Path,
    output_name: str,
    overwrite: bool,
) -> Path:
    entry = pick_video_entry(record)
    if str(entry.get("type", "")) != "output":
        raise RuntimeError(
            f"Expected an output video file, got type={entry.get('type')}. "
            "Set the workflow save node to write to ComfyUI output."
        )

    subfolder = str(entry.get("subfolder", "")).strip("/\\")
    source_path = comfyui_output_dir / subfolder / str(entry["filename"])
    if not source_path.exists():
        raise FileNotFoundError(f"Generated video not found on disk: {source_path}")

    outputs_dir.mkdir(parents=True, exist_ok=True)
    destination = outputs_dir / output_name
    if destination.exists() and not overwrite:
        raise FileExistsError(
            f"Output already exists: {destination}. Use --overwrite to replace it."
        )

    shutil.copy2(source_path, destination)
    return destination


def seed_for_item(item: dict[str, Any], seed_base: int) -> int:
    if "seed" in item and str(item["seed"]).strip() != "":
        return int(item["seed"])
    return seed_base + int(item["index"])


def validate_workflow_config(config_path: Path) -> dict[str, Any]:
    root = project_root()
    config = normalize_config(load_json(config_path))
    workflow_path = resolve_path(root, config["workflow_path"])
    raw_workflow = load_json(workflow_path)
    base_url = str(config.get("comfyui_base_url", "")).rstrip("/")
    session = requests.Session()
    workflow_template = normalize_workflow_template(
        raw_workflow,
        base_url=base_url,
        session=session,
    )
    bindings = resolve_bindings(workflow_template, config)
    return bindings


def run_batch(
    options: BatchRunOptions,
    on_log: LogCallback | None = None,
    on_progress: ProgressCallback | None = None,
    is_cancelled_callback: CancelCallback | None = None,
) -> BatchSummary:
    root = project_root()
    config = normalize_config(load_json(options.config_path))

    workflow_path = resolve_path(root, config["workflow_path"])
    prompts_path = resolve_path(root, config["prompts_path"])
    cells_dir = resolve_path(root, config["cells_dir"])
    outputs_dir = resolve_path(root, config["outputs_dir"])
    failed_jobs_path = resolve_path(root, config["failed_jobs_path"])

    raw_workflow = load_json(workflow_path)
    base_url = str(config["comfyui_base_url"]).rstrip("/")
    session = requests.Session()
    workflow_template = normalize_workflow_template(
        raw_workflow,
        base_url=base_url,
        session=session,
    )
    bindings = resolve_bindings(workflow_template, config)

    prompts = load_prompts(load_json(prompts_path))
    selected_prompts = filter_prompts(prompts, options.start_index, options.end_index)
    if not selected_prompts:
        raise ValueError("No prompt rows match the selected start/end range.")

    comfyui_output_dir = Path(str(config["comfyui_output_dir"]))
    if is_placeholder(comfyui_output_dir):
        raise ValueError("Please set comfyui_output_dir in config/workflow_config.json.")

    poll_interval = (
        options.poll_interval
        if options.poll_interval is not None
        else float(config.get("poll_interval_seconds", 5))
    )
    timeout_seconds = (
        options.timeout_seconds
        if options.timeout_seconds is not None
        else int(config.get("timeout_seconds", 3600))
    )
    seed_base = int(config.get("seed_base", 1))
    prefix_root = str(config.get("save_prefix_root", "video/ltx_storyboard_batch")).strip("/\\")
    upload_images = bool(config.get("upload_images", True))
    upload_subfolder = str(config.get("upload_subfolder", ""))

    failures: list[dict[str, Any]] = []
    outputs: list[str] = []
    completed = 0
    skipped = 0
    cancelled = 0
    queued_jobs: list[dict[str, Any]] = []
    finalized_indices: set[int] = set()
    stop_requested = False

    emit_log(on_log, "Resolved workflow bindings:")
    emit_log(on_log, json.dumps(bindings, ensure_ascii=False, indent=2))
    emit_progress(
        on_progress,
        {
            "event": "initialized",
            "bindings": bindings,
            "total": len(selected_prompts),
            "start_index": options.start_index,
            "end_index": options.end_index,
        },
    )

    def mark_cancelled(index: int, output_name: str, reason: str = "Stopped by user.") -> None:
        nonlocal cancelled
        if index in finalized_indices:
            return
        finalized_indices.add(index)
        cancelled += 1
        emit_log(on_log, f"[CANCEL] index={index:02d} output={output_name} reason={reason}")
        emit_progress(
            on_progress,
            {
                "event": "job_finished",
                "index": index,
                "status": "cancelled",
                "output_name": output_name,
                "error": reason,
            },
        )

    for position, item in enumerate(selected_prompts, start=1):
        if is_cancelled(is_cancelled_callback):
            stop_requested = True
            emit_log(on_log, "[STOP] Batch stop requested. Halting new submissions.")
            break

        index = int(item["index"])
        cell_path = cells_dir / f"{index:02d}.png"
        output_name = build_output_name(item)
        output_path = outputs_dir / output_name
        emit_progress(
            on_progress,
            {
                "event": "job_started",
                "position": position,
                "index": index,
                "total": len(selected_prompts),
                "output_name": output_name,
            },
        )

        if output_path.exists() and not options.overwrite:
            skipped += 1
            emit_log(on_log, f"[SKIP] {output_name} already exists.")
            emit_progress(
                on_progress,
                {
                    "event": "job_finished",
                    "index": index,
                    "status": "skipped",
                    "output_name": output_name,
                },
            )
            continue

        try:
            if is_cancelled(is_cancelled_callback):
                raise BatchCancelled("Batch stop requested before queue submission.")
            if not cell_path.exists():
                raise FileNotFoundError(f"Cell image not found: {cell_path}")

            workflow = copy.deepcopy(workflow_template)
            seed_value = seed_for_item(item, seed_base)
            prefix_name = sanitize_prefix(output_name)
            filename_prefix = f"{prefix_root}/{prefix_name}" if prefix_root else prefix_name

            if upload_images:
                upload_result = upload_image(session, base_url, cell_path, upload_subfolder)
                image_value = uploaded_image_value(upload_result)
            else:
                image_value = str(cell_path.resolve())

            if is_cancelled(is_cancelled_callback):
                raise BatchCancelled("Batch stop requested before prompt submission.")

            apply_batch_values(
                workflow=workflow,
                bindings=bindings,
                image_value=image_value,
                prompt_text=str(item["prompt"]),
                seed_value=seed_value,
                filename_prefix=filename_prefix,
            )

            emit_log(on_log, f"[QUEUE] index={index:02d} seed={seed_value} output={output_name}")
            prompt_id = submit_prompt(session, base_url, workflow)
            queued_jobs.append(
                {
                    "position": position,
                    "index": index,
                    "output_name": output_name,
                    "prompt_id": prompt_id,
                    "cell_path": str(cell_path),
                    "prompt": item.get("prompt", ""),
                }
            )
            emit_progress(
                on_progress,
                {
                    "event": "job_submitted",
                    "position": position,
                    "index": index,
                    "prompt_id": prompt_id,
                    "output_name": output_name,
                },
            )
        except BatchCancelled as exc:
            stop_requested = True
            emit_log(on_log, f"[STOP] index={index:02d} output={output_name} reason={exc}")
            mark_cancelled(index, output_name, str(exc))
            break
        except Exception as exc:
            failure = {
                "index": index,
                "output_name": output_name,
                "cell_path": str(cell_path),
                "prompt": item.get("prompt", ""),
                "error": str(exc),
                "timestamp": int(time.time()),
                "phase": "submit",
            }
            failures.append(failure)
            finalized_indices.add(index)
            emit_log(on_log, f"[FAIL] index={index:02d} output={output_name} error={exc}")
            emit_progress(
                on_progress,
                {
                    "event": "job_finished",
                    "index": index,
                    "status": "failed",
                    "output_name": output_name,
                    "error": str(exc),
                },
            )

    if stop_requested:
        for job in queued_jobs:
            mark_cancelled(int(job["index"]), str(job["output_name"]))
    elif queued_jobs:
        emit_log(
            on_log,
            f"[QUEUE] Submitted {len(queued_jobs)} job(s) to ComfyUI. Waiting for completions...",
        )

    for queue_position, job in enumerate(queued_jobs, start=1):
        if stop_requested:
            break

        index = int(job["index"])
        output_name = str(job["output_name"])
        prompt_id = str(job["prompt_id"])

        if index in finalized_indices:
            continue

        if is_cancelled(is_cancelled_callback):
            stop_requested = True
            emit_log(on_log, "[STOP] Batch stop requested. Cancelling queued jobs.")
            for pending in queued_jobs[queue_position - 1 :]:
                mark_cancelled(int(pending["index"]), str(pending["output_name"]))
            break

        emit_progress(
            on_progress,
            {
                "event": "job_running",
                "queue_position": queue_position,
                "queue_total": len(queued_jobs),
                "index": index,
                "prompt_id": prompt_id,
                "output_name": output_name,
            },
        )
        emit_log(
            on_log,
            f"[WAIT] ({queue_position}/{len(queued_jobs)}) prompt_id={prompt_id} output={output_name}",
        )

        try:
            record = wait_for_completion(
                session=session,
                base_url=base_url,
                prompt_id=prompt_id,
                poll_interval_seconds=poll_interval,
                timeout_seconds=timeout_seconds,
                is_cancelled_callback=is_cancelled_callback,
            )
            if is_cancelled(is_cancelled_callback):
                raise BatchCancelled(f"Batch stop requested after prompt_id={prompt_id} completed.")
            saved_path = copy_video_to_project(
                record=record,
                comfyui_output_dir=comfyui_output_dir,
                outputs_dir=outputs_dir,
                output_name=output_name,
                overwrite=options.overwrite,
            )
            outputs.append(str(saved_path))
            completed += 1
            finalized_indices.add(index)
            emit_log(on_log, f"[DONE] prompt_id={prompt_id} -> {saved_path}")
            emit_progress(
                on_progress,
                {
                    "event": "job_finished",
                    "index": index,
                    "status": "completed",
                    "output_name": output_name,
                    "saved_path": str(saved_path),
                },
            )
        except BatchCancelled as exc:
            stop_requested = True
            emit_log(on_log, f"[STOP] prompt_id={prompt_id} output={output_name} reason={exc}")
            for pending in queued_jobs[queue_position - 1 :]:
                mark_cancelled(int(pending["index"]), str(pending["output_name"]), "Stopped by user.")
            break
        except Exception as exc:
            failure = {
                "index": index,
                "output_name": output_name,
                "cell_path": str(job["cell_path"]),
                "prompt": job.get("prompt", ""),
                "prompt_id": prompt_id,
                "error": str(exc),
                "timestamp": int(time.time()),
                "phase": "wait",
            }
            failures.append(failure)
            finalized_indices.add(index)
            emit_log(on_log, f"[FAIL] index={index:02d} output={output_name} error={exc}")
            emit_progress(
                on_progress,
                {
                    "event": "job_finished",
                    "index": index,
                    "status": "failed",
                    "output_name": output_name,
                    "error": str(exc),
                },
            )

    save_json(failed_jobs_path, failures)

    summary = BatchSummary(
        total=len(selected_prompts),
        completed=completed,
        failed=len(failures),
        skipped=skipped,
        cancelled=cancelled,
        failures=failures,
        bindings=bindings,
        outputs=outputs,
    )
    emit_progress(
        on_progress,
        {
            "event": "complete",
            "summary": summary.to_dict(),
            "stopped": stop_requested or is_cancelled(is_cancelled_callback),
        },
    )
    return summary
