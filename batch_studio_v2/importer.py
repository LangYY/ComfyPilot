from __future__ import annotations

from pathlib import Path
from typing import Any

from ltx_batch.project import load_json as legacy_load_json
from ltx_batch.project import normalize_config as legacy_normalize_config
from ltx_batch.project import resolve_path as legacy_resolve_path

from .store import StudioStore


def legacy_project_inputs(legacy_root: Path) -> dict[str, Any] | None:
    config_path = legacy_root / "config" / "workflow_config.json"
    prompts_path = legacy_root / "data" / "prompts.json"
    workflow_path = legacy_root / "workflows" / "ltx_i2v_api.json"

    if not config_path.exists() or not prompts_path.exists() or not workflow_path.exists():
        return None

    config = legacy_normalize_config(legacy_load_json(config_path))
    storyboard_path = legacy_root / "storyboard_3x4.png"
    resolved_workflow_path = legacy_resolve_path(legacy_root, config["workflow_path"])
    resolved_prompts_path = legacy_resolve_path(legacy_root, config["prompts_path"])

    return {
        "config": config,
        "config_path": config_path,
        "workflow_path": resolved_workflow_path,
        "prompts_path": resolved_prompts_path,
        "storyboard_path": storyboard_path if storyboard_path.exists() else None,
    }


def import_legacy_project(store: StudioStore, legacy_root: Path, *, force: bool = False) -> dict[str, Any]:
    legacy_root = legacy_root.resolve()
    available = legacy_project_inputs(legacy_root)
    if not available:
        raise FileNotFoundError(
            f"Legacy project inputs were not found under: {legacy_root}"
        )

    for existing in store.list_projects():
        if str(existing.get("legacy_root", "")).strip().lower() == str(legacy_root).lower():
            existing_detail = store.project_detail(existing["id"])
            is_incomplete = not existing_detail["profiles"] and not existing_detail["drafts"]
            if is_incomplete:
                store.delete_project(existing["id"])
                break
            if not force:
                return existing

    config = available["config"]
    project = store.create_project(
        name=f"{legacy_root.name} imported",
        comfyui_base_url=str(config.get("comfyui_base_url", "")).rstrip("/"),
        comfyui_output_dir=str(config.get("comfyui_output_dir", "")),
        legacy_root=str(legacy_root),
    )
    try:
        project["default_run_settings"].update(
            {
                "seed_base": int(config.get("seed_base", 1)),
                "save_prefix_root": str(config.get("save_prefix_root", "video/batch_studio_v2")),
                "upload_subfolder": str(config.get("upload_subfolder", "")),
                "upload_files": bool(config.get("upload_images", True)),
                "poll_interval_seconds": float(config.get("poll_interval_seconds", 5)),
                "timeout_seconds": int(config.get("timeout_seconds", 3600)),
            }
        )
        store.save_project(project)

        workflow_text = available["workflow_path"].read_text(encoding="utf-8")
        profile = store.create_profile_from_text(
            project_id=project["id"],
            name="Imported legacy workflow",
            workflow_text=workflow_text,
            config_hint=config,
        )

        prompts_text = available["prompts_path"].read_text(encoding="utf-8")
        storyboard_path = available.get("storyboard_path")
        if storyboard_path and storyboard_path.exists():
            store.create_storyboard_draft(
                project_id=project["id"],
                profile_id=profile["id"],
                prompts_text=prompts_text,
                storyboard_name=storyboard_path.name,
                storyboard_bytes=storyboard_path.read_bytes(),
                rows=4,
                cols=3,
                margin=0,
                gutter=0,
                runtime_overrides={},
            )
        else:
            store.create_prompt_only_draft(
                project_id=project["id"],
                profile_id=profile["id"],
                prompts_text=prompts_text,
                runtime_overrides={},
            )
        return store.load_project(project["id"])
    except Exception:
        store.delete_project(project["id"])
        raise


def bootstrap_legacy_import(store: StudioStore, legacy_root: Path) -> dict[str, Any] | None:
    existing_projects = store.list_projects()
    if existing_projects:
        for existing in existing_projects:
            if str(existing.get("legacy_root", "")).strip().lower() != str(legacy_root.resolve()).lower():
                continue
            detail = store.project_detail(existing["id"])
            if detail["profiles"] or detail["drafts"]:
                return existing
        if any(
            str(item.get("legacy_root", "")).strip().lower() == str(legacy_root.resolve()).lower()
            for item in existing_projects
        ):
            return import_legacy_project(store, legacy_root, force=True)
        return None
    available = legacy_project_inputs(legacy_root)
    if not available:
        return None
    return import_legacy_project(store, legacy_root, force=False)
