from __future__ import annotations

import json
import random
import re
import shutil
from pathlib import Path
from typing import Any

from ltx_batch.batch import build_output_name
from ltx_batch.storyboard import split_storyboard

from .common import (
    copy_file,
    copy_tree,
    ensure_dir,
    file_url,
    load_json,
    load_text,
    make_id,
    now_iso,
    save_json,
    save_text,
    slugify,
    to_relative_string,
)
from .profiles import inspect_workflow_profile, raw_workflow_from_text
from .prompts import compute_seed, input_ref, merge_run_settings, normalize_prompt_payload_text, parse_prompt_payload


class StudioStore:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root.resolve()
        self.store_root = self.repo_root / "studio_v2_data"
        self.projects_root = self.store_root / "projects"
        self.queue_path = self.store_root / "queue.json"
        ensure_dir(self.projects_root)
        if not self.queue_path.exists():
            self.save_queue_state(
                {
                    "current": None,
                    "queued": [],
                    "updated_at": now_iso(),
                }
            )

    def get_or_create_default_project(self) -> dict[str, Any]:
        projects = self.list_projects()
        if projects:
            return projects[0]
        return self.create_project(
            name="本地批量生成",
            comfyui_base_url="http://127.0.0.1:8189",
            comfyui_output_dir="",
        )

    def update_project_settings(
        self,
        project_id: str,
        *,
        comfyui_base_url: str,
        comfyui_output_dir: str,
        default_run_settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.load_project(project_id)
        project.setdefault("comfyui", {})
        project["comfyui"]["base_url"] = str(comfyui_base_url).rstrip("/")
        project["comfyui"]["output_dir"] = str(comfyui_output_dir).strip()
        if default_run_settings:
            merged = dict(project.get("default_run_settings", {}))
            merged.update(default_run_settings)
            project["default_run_settings"] = merged
        self.save_project(project)
        return project

    def file_url_for_path(self, path: Path) -> str:
        return file_url(to_relative_string(self.store_root, path))

    def load_queue_state(self) -> dict[str, Any]:
        return load_json(
            self.queue_path,
            {
                "current": None,
                "queued": [],
                "updated_at": now_iso(),
            },
        )

    def save_queue_state(self, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        payload["updated_at"] = now_iso()
        save_json(self.queue_path, payload)

    def _project_dir(self, project_id: str) -> Path:
        return self.projects_root / project_id

    def _project_manifest_path(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "project.json"

    def _profiles_root(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "profiles"

    def _profile_dir(self, project_id: str, profile_id: str) -> Path:
        return self._profiles_root(project_id) / profile_id

    def _profile_manifest_path(self, project_id: str, profile_id: str) -> Path:
        return self._profile_dir(project_id, profile_id) / "profile.json"

    def _drafts_root(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "drafts"

    def _draft_dir(self, project_id: str, draft_id: str) -> Path:
        return self._drafts_root(project_id) / draft_id

    def _draft_manifest_path(self, project_id: str, draft_id: str) -> Path:
        return self._draft_dir(project_id, draft_id) / "draft.json"

    def _batches_root(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "batches"

    def _batch_dir(self, project_id: str, batch_id: str) -> Path:
        return self._batches_root(project_id) / batch_id

    def _batch_manifest_path(self, project_id: str, batch_id: str) -> Path:
        return self._batch_dir(project_id, batch_id) / "batch.json"

    def _runs_root(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "runs"

    def _run_dir(self, project_id: str, run_id: str) -> Path:
        return self._runs_root(project_id) / run_id

    def _run_manifest_path(self, project_id: str, run_id: str) -> Path:
        return self._run_dir(project_id, run_id) / "run.json"

    def create_project(
        self,
        *,
        name: str,
        comfyui_base_url: str,
        comfyui_output_dir: str,
        legacy_root: str | None = None,
    ) -> dict[str, Any]:
        project_id = make_id("project")
        project_dir = self._project_dir(project_id)
        ensure_dir(project_dir)
        ensure_dir(self._profiles_root(project_id))
        ensure_dir(self._drafts_root(project_id))
        ensure_dir(self._batches_root(project_id))
        ensure_dir(self._runs_root(project_id))

        manifest = {
            "id": project_id,
            "name": name.strip() or "Untitled Project",
            "slug": slugify(name, default="project"),
            "kind": "video_batch_project",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "default_profile_id": None,
            "legacy_root": legacy_root or "",
            "comfyui": {
                "base_url": str(comfyui_base_url).rstrip("/"),
                "output_dir": str(comfyui_output_dir).strip(),
            },
            "default_run_settings": {
                "seed_base": 1,
                "final_output_dir": "",
                "save_prefix_root": "batch_studio_v2",
                "output_name_prefix": "",
                "repeat_count": 1,
                "width_pixels": None,
                "height_pixels": None,
                "duration_seconds": None,
                "upload_subfolder": "",
                "upload_files": True,
                "poll_interval_seconds": 5,
                "timeout_seconds": 3600,
                "task_cooldown_seconds": 10,
                "maintenance_interval_tasks": 5,
                "maintenance_cooldown_seconds": 20,
                "maintenance_memory_mode": "free_memory",
                "overwrite_outputs": False,
                "negative_prompt_text": "",
                "seed_mode": "fixed",
                "seed_fixed": 1,
            },
            "template_presets": [],
        }
        save_json(self._project_manifest_path(project_id), manifest)
        return manifest

    def list_projects(self) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        for path in sorted(self.projects_root.glob("*/project.json")):
            project = load_json(path)
            if project:
                projects.append(project)
        return sorted(projects, key=lambda item: item.get("updated_at", ""), reverse=True)

    def load_project(self, project_id: str) -> dict[str, Any]:
        project = load_json(self._project_manifest_path(project_id))
        if not project:
            raise FileNotFoundError(f"Project not found: {project_id}")
        return project

    def save_project(self, manifest: dict[str, Any]) -> None:
        manifest = dict(manifest)
        manifest["updated_at"] = now_iso()
        save_json(self._project_manifest_path(manifest["id"]), manifest)

    def delete_project(self, project_id: str) -> None:
        project_dir = self._project_dir(project_id)
        if project_dir.exists():
            shutil.rmtree(project_dir)

    def list_profiles(self, project_id: str) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for path in sorted(self._profiles_root(project_id).glob("*/profile.json")):
            profile = load_json(path)
            if profile:
                profiles.append(profile)
        return sorted(profiles, key=lambda item: item.get("updated_at", ""), reverse=True)

    def load_profile(self, project_id: str, profile_id: str) -> dict[str, Any]:
        manifest = load_json(self._profile_manifest_path(project_id, profile_id))
        if not manifest:
            raise FileNotFoundError(f"Workflow profile not found: {project_id}/{profile_id}")
        return manifest

    def create_profile_from_text(
        self,
        *,
        project_id: str,
        name: str,
        workflow_text: str,
        config_hint: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.load_project(project_id)
        profile_id = make_id("profile")
        profile_dir = self._profile_dir(project_id, profile_id)
        ensure_dir(profile_dir)

        raw_path = profile_dir / "workflow_source.json"
        compiled_path = profile_dir / "compiled_prompt.json"
        raw_workflow = raw_workflow_from_text(workflow_text)
        manifest, compiled_template = inspect_workflow_profile(
            workflow_data=raw_workflow,
            base_url=project["comfyui"]["base_url"],
            config_hint=config_hint or {},
        )
        manifest.update(
            {
                "id": profile_id,
                "project_id": project_id,
                "name": name.strip() or "Workflow Profile",
                "source": {
                    "workflow_json_path": to_relative_string(profile_dir, raw_path),
                    "compiled_workflow_path": to_relative_string(profile_dir, compiled_path),
                },
            }
        )
        save_text(raw_path, workflow_text.strip() + "\n")
        save_json(compiled_path, compiled_template)
        save_json(self._profile_manifest_path(project_id, profile_id), manifest)

        if not project.get("default_profile_id"):
            project["default_profile_id"] = profile_id
            self.save_project(project)
        return manifest

    def load_compiled_profile_workflow(self, project_id: str, profile_id: str) -> dict[str, dict[str, Any]]:
        profile = self.load_profile(project_id, profile_id)
        compiled_path = self._profile_dir(project_id, profile_id) / profile["source"]["compiled_workflow_path"]
        compiled = load_json(compiled_path)
        if not compiled:
            raise FileNotFoundError(f"Compiled workflow not found for profile: {profile_id}")
        return compiled

    def list_drafts(self, project_id: str) -> list[dict[str, Any]]:
        drafts: list[dict[str, Any]] = []
        for path in sorted(self._drafts_root(project_id).glob("*/draft.json")):
            draft = load_json(path)
            if draft:
                drafts.append(draft)
        return sorted(drafts, key=lambda item: item.get("updated_at", ""), reverse=True)

    def load_draft(self, project_id: str, draft_id: str) -> dict[str, Any]:
        manifest = load_json(self._draft_manifest_path(project_id, draft_id))
        if not manifest:
            raise FileNotFoundError(f"Draft not found: {project_id}/{draft_id}")
        return manifest

    def save_draft(self, manifest: dict[str, Any]) -> None:
        manifest = dict(manifest)
        manifest["updated_at"] = now_iso()
        save_json(self._draft_manifest_path(manifest["project_id"], manifest["id"]), manifest)

    def list_batches(self, project_id: str) -> list[dict[str, Any]]:
        batches: list[dict[str, Any]] = []
        for path in sorted(self._batches_root(project_id).glob("*/batch.json")):
            batch = load_json(path)
            if batch:
                batches.append(batch)
        return sorted(batches, key=lambda item: item.get("created_at", ""), reverse=True)

    def load_batch(self, project_id: str, batch_id: str) -> dict[str, Any]:
        manifest = load_json(self._batch_manifest_path(project_id, batch_id))
        if not manifest:
            raise FileNotFoundError(f"Batch snapshot not found: {project_id}/{batch_id}")
        return manifest

    def save_batch(self, manifest: dict[str, Any]) -> None:
        manifest = dict(manifest)
        manifest["updated_at"] = now_iso()
        save_json(self._batch_manifest_path(manifest["project_id"], manifest["id"]), manifest)

    def update_batch_status(
        self,
        project_id: str,
        batch_id: str,
        status: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        batch = self.load_batch(project_id, batch_id)
        batch["status"] = status
        if extra:
            batch.update(extra)
        self.save_batch(batch)
        return batch

    def schedule_batch(self, project_id: str, batch_id: str, run_at: str) -> dict[str, Any]:
        batch = self.load_batch(project_id, batch_id)
        batch["status"] = "scheduled"
        batch["schedule"] = {
            "run_at": str(run_at).strip(),
            "created_at": now_iso(),
            "status": "waiting",
        }
        self.save_batch(batch)
        return batch

    def delete_planned_batch(self, project_id: str, batch_id: str) -> dict[str, Any]:
        batch = self.load_batch(project_id, batch_id)
        if batch.get("status") not in {"planned", "scheduled"}:
            return {
                "deleted": False,
                "reason": "not_in_plan",
                "status": batch.get("status", ""),
            }
        batch_dir = self._batch_dir(project_id, batch_id)
        if batch_dir.exists():
            shutil.rmtree(batch_dir)
        return {"deleted": True, "reason": "deleted"}

    def list_runs(self, project_id: str) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for path in sorted(self._runs_root(project_id).glob("*/run.json")):
            run = load_json(path)
            if run:
                runs.append(run)
        return sorted(runs, key=lambda item: item.get("created_at", ""), reverse=True)

    def load_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        manifest = load_json(self._run_manifest_path(project_id, run_id))
        if not manifest:
            raise FileNotFoundError(f"Run not found: {project_id}/{run_id}")
        return manifest

    def save_run(self, manifest: dict[str, Any]) -> None:
        manifest = dict(manifest)
        manifest["updated_at"] = now_iso()
        save_json(self._run_manifest_path(manifest["project_id"], manifest["id"]), manifest)

    def _normalize_prompts_payload(self, prompts_text: str) -> Any:
        return normalize_prompt_payload_text(prompts_text)

    def _natural_file_key(self, name: str) -> list[Any]:
        parts = re.split(r"(\d+)", Path(name).stem)
        key: list[Any] = []
        for part in parts:
            key.append(int(part) if part.isdigit() else part.lower())
        return key

    def _apply_seed_policy(self, tasks: list[dict[str, Any]], runtime_settings: dict[str, Any]) -> None:
        seed_mode = str(runtime_settings.get("seed_mode", "fixed")).strip().lower()
        if seed_mode == "random":
            for task in tasks:
                task["seed_value"] = random.randint(1, 2_147_483_647)
            return
        if seed_mode == "fixed":
            fixed_seed = int(runtime_settings.get("seed_fixed") or runtime_settings.get("seed_base") or 1)
            for task in tasks:
                task["seed_value"] = self._seed_for_draw(fixed_seed, task)

    def _repeat_count(self, runtime_settings: dict[str, Any]) -> int:
        try:
            repeat_count = int(runtime_settings.get("repeat_count") or 1)
        except (TypeError, ValueError):
            repeat_count = 1
        return max(1, min(repeat_count, 20))

    def _seed_for_draw(self, seed_value: int, task: dict[str, Any]) -> int:
        draw_count = int(task.get("draw_count") or 1)
        draw_index = int(task.get("draw_index") or 1)
        if draw_count <= 1:
            return int(seed_value)
        return int(seed_value) + max(0, draw_index - 1)

    def _output_name_for_draw(self, output_name: str, draw_index: int, draw_count: int) -> str:
        if draw_count <= 1:
            return output_name
        name_path = Path(output_name)
        suffix = name_path.suffix or ".mp4"
        draw_name = f"{name_path.stem}_draw{draw_index:02d}{suffix}"
        if str(name_path.parent) in {"", "."}:
            return draw_name
        return f"{name_path.parent.as_posix()}/{draw_name}"

    def _clone_task(self, task: Any) -> Any:
        return json.loads(json.dumps(task, ensure_ascii=False))

    def _expand_tasks_for_repeats(
        self,
        base_tasks: list[dict[str, Any]],
        runtime_settings: dict[str, Any],
    ) -> list[dict[str, Any]]:
        repeat_count = self._repeat_count(runtime_settings)
        seed_mode = str(runtime_settings.get("seed_mode", "fixed")).strip().lower()
        expanded: list[dict[str, Any]] = []
        order = 1
        for base_task in base_tasks:
            source_task_id = str(base_task.get("source_task_id") or base_task.get("task_id") or make_id("task"))
            for draw_index in range(1, repeat_count + 1):
                task = self._clone_task(base_task)
                if repeat_count > 1:
                    task["task_id"] = make_id("task")
                task["order"] = order
                task["source_order"] = int(base_task.get("source_order") or base_task.get("order") or order)
                task["source_task_id"] = source_task_id
                task["draw_index"] = draw_index
                task["draw_count"] = repeat_count
                task["expected_output_name"] = self._output_name_for_draw(
                    str(task.get("expected_output_name") or "output.mp4"),
                    draw_index,
                    repeat_count,
                )
                if seed_mode == "random":
                    task["seed_value"] = random.randint(1, 2_147_483_647)
                else:
                    task["seed_value"] = self._seed_for_draw(int(task.get("seed_value") or 1), task)
                expanded.append(task)
                order += 1
        return expanded

    def _merge_submission_runtime_settings(
        self,
        base: dict[str, Any],
        overrides: dict[str, Any] | None,
    ) -> dict[str, Any]:
        clearable_string_keys = {"save_prefix_root", "output_name_prefix", "negative_prompt_text"}
        merged = dict(base or {})
        for key, value in (overrides or {}).items():
            if value is None:
                continue
            if value == "" and key not in clearable_string_keys:
                continue
            merged[key] = value
        return merged

    def _task_prompt_entry(self, task: dict[str, Any]) -> dict[str, Any]:
        entry = dict(task.get("sidecar") or {})
        entry.setdefault("index", task.get("source_index") or task.get("order") or 1)
        entry["prompt"] = task.get("prompt_text", "")
        return entry

    def _refresh_task_runtime_fields(
        self,
        tasks: list[dict[str, Any]],
        runtime_settings: dict[str, Any],
        *,
        update_seeds: bool = False,
        update_output_names: bool = True,
    ) -> None:
        if update_output_names:
            output_name_prefix = str(runtime_settings.get("output_name_prefix", "")).strip()
            for task in tasks:
                entry = self._task_prompt_entry(task)
                base_output_name = str(task.get("base_output_name") or build_output_name(entry))
                task["base_output_name"] = base_output_name
                expected_output_name = base_output_name
                if output_name_prefix:
                    name_path = Path(base_output_name)
                    expected_output_name = f"{output_name_prefix}{name_path.stem}{name_path.suffix}"
                expected_output_name = self._output_name_for_draw(
                    expected_output_name,
                    int(task.get("draw_index") or 1),
                    int(task.get("draw_count") or 1),
                )
                task["expected_output_name"] = expected_output_name

        if update_seeds:
            seed_base = int(runtime_settings.get("seed_base") or 1)
            for task in tasks:
                entry = self._task_prompt_entry(task)
                order = int(task.get("order") or 1)
                task["seed_value"] = compute_seed(entry, order, seed_base)
                task["seed_value"] = self._seed_for_draw(int(task["seed_value"]), task)
            self._apply_seed_policy(tasks, runtime_settings)

    def _write_uploaded_images(
        self,
        target_dir: Path,
        files: list[tuple[str, bytes]],
    ) -> list[Path]:
        ensure_dir(target_dir)
        written: list[Path] = []
        for position, (name, raw) in enumerate(
            sorted(files, key=lambda item: self._natural_file_key(item[0])),
            start=1,
        ):
            source_name = Path(name).name or f"image_{position:03d}.png"
            suffix = Path(source_name).suffix or ".png"
            path = target_dir / f"{position:03d}_{Path(source_name).stem}{suffix}"
            path.write_bytes(raw)
            written.append(path)
        return written

    def create_prompt_only_draft(
        self,
        *,
        project_id: str,
        profile_id: str,
        prompts_text: str,
        runtime_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.load_project(project_id)
        profile = self.load_profile(project_id, profile_id)
        draft_id = make_id("draft")
        draft_dir = self._draft_dir(project_id, draft_id)
        source_dir = ensure_dir(draft_dir / "source")

        prompts_path = source_dir / "prompts.json"
        save_text(prompts_path, prompts_text.strip() + "\n")

        runtime_settings = merge_run_settings(project["default_run_settings"], profile.get("defaults", {}))
        runtime_settings = merge_run_settings(runtime_settings, runtime_overrides)
        runtime_settings["draft_mode"] = "t2v"
        tasks = parse_prompt_payload(
            self._normalize_prompts_payload(prompts_text),
            seed_base=int(runtime_settings.get("seed_base", 1)),
            output_name_prefix=str(runtime_settings.get("output_name_prefix", "")),
        )
        self._apply_seed_policy(tasks, runtime_settings)
        base_tasks = self._clone_task(tasks)
        tasks = self._expand_tasks_for_repeats(base_tasks, runtime_settings)

        manifest = {
            "id": draft_id,
            "project_id": project_id,
            "profile_id": profile_id,
            "status": "draft",
            "source_kind": "prompt_only",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "runtime_settings": runtime_settings,
            "source_files": {
                "prompts_json": to_relative_string(draft_dir, prompts_path),
            },
            "split_config": None,
            "base_tasks": base_tasks,
            "tasks": tasks,
            "task_count": len(tasks),
            "source_task_count": len(base_tasks),
        }
        save_json(self._draft_manifest_path(project_id, draft_id), manifest)
        return manifest

    def create_storyboard_draft(
        self,
        *,
        project_id: str,
        profile_id: str,
        prompts_text: str,
        storyboard_name: str,
        storyboard_bytes: bytes,
        rows: int,
        cols: int,
        cell_count: int | None,
        margin: float,
        gutter: float,
        runtime_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.load_project(project_id)
        profile = self.load_profile(project_id, profile_id)
        if profile["input_contract"]["primary_media_kind"] != "image":
            raise ValueError("The selected workflow profile does not expect an image primary input.")

        draft_id = make_id("draft")
        draft_dir = self._draft_dir(project_id, draft_id)
        source_dir = ensure_dir(draft_dir / "source")
        inputs_dir = ensure_dir(draft_dir / "inputs")

        prompts_path = source_dir / "prompts.json"
        storyboard_path = source_dir / (Path(storyboard_name).name or "storyboard.png")
        save_text(prompts_path, prompts_text.strip() + "\n")
        storyboard_path.write_bytes(storyboard_bytes)

        cells = split_storyboard(
            storyboard_path=storyboard_path,
            output_dir=inputs_dir,
            rows=rows,
            cols=cols,
            margin=margin,
            gutter=gutter,
        )
        total_cells = len(cells)
        if cell_count is not None:
            if cell_count < 1 or cell_count > total_cells:
                raise ValueError(
                    f"cell_count must be between 1 and {total_cells}; got {cell_count}."
                )
            cells = cells[:cell_count]

        input_refs_by_order: dict[int, list[dict[str, Any]]] = {}
        for order, cell in enumerate(cells, start=1):
            relative_path = to_relative_string(draft_dir, cell.output_path)
            input_refs_by_order[order] = [input_ref("image", relative_path, f"cell_{order:02d}")]

        runtime_settings = merge_run_settings(project["default_run_settings"], profile.get("defaults", {}))
        runtime_settings = merge_run_settings(runtime_settings, runtime_overrides)
        runtime_settings["draft_mode"] = "i2v_storyboard"
        tasks = parse_prompt_payload(
            self._normalize_prompts_payload(prompts_text),
            seed_base=int(runtime_settings.get("seed_base", 1)),
            output_name_prefix=str(runtime_settings.get("output_name_prefix", "")),
            input_refs_by_order=input_refs_by_order,
        )
        self._apply_seed_policy(tasks, runtime_settings)

        if len(tasks) != len(cells):
            raise ValueError(
                f"Storyboard split produced {len(cells)} cells but prompts expanded to {len(tasks)} tasks. "
                "For storyboard-grid drafts the counts must match."
            )
        base_tasks = self._clone_task(tasks)
        tasks = self._expand_tasks_for_repeats(base_tasks, runtime_settings)

        manifest = {
            "id": draft_id,
            "project_id": project_id,
            "profile_id": profile_id,
            "status": "draft",
            "source_kind": "storyboard_grid",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "runtime_settings": runtime_settings,
            "source_files": {
                "prompts_json": to_relative_string(draft_dir, prompts_path),
                "storyboard": to_relative_string(draft_dir, storyboard_path),
            },
            "split_config": {
                "rows": int(rows),
                "cols": int(cols),
                "margin": float(margin),
                "gutter": float(gutter),
                "cell_count": len(cells),
                "grid_cell_count": total_cells,
            },
            "base_tasks": base_tasks,
            "tasks": tasks,
            "task_count": len(tasks),
            "source_task_count": len(base_tasks),
        }
        save_json(self._draft_manifest_path(project_id, draft_id), manifest)
        return manifest

    def create_image_batch_draft(
        self,
        *,
        project_id: str,
        profile_id: str,
        prompts_text: str,
        image_files: list[tuple[str, bytes]],
        runtime_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.load_project(project_id)
        profile = self.load_profile(project_id, profile_id)
        if profile["input_contract"]["primary_media_kind"] != "image":
            raise ValueError("The selected workflow profile does not expect image inputs.")
        if not image_files:
            raise ValueError("At least one first-frame image is required.")

        draft_id = make_id("draft")
        draft_dir = self._draft_dir(project_id, draft_id)
        source_dir = ensure_dir(draft_dir / "source")
        inputs_dir = ensure_dir(draft_dir / "inputs" / "first")

        prompts_path = source_dir / "prompts.json"
        save_text(prompts_path, prompts_text.strip() + "\n")
        images = self._write_uploaded_images(inputs_dir, image_files)

        input_refs_by_order: dict[int, list[dict[str, Any]]] = {}
        for order, image_path in enumerate(images, start=1):
            relative_path = to_relative_string(draft_dir, image_path)
            input_refs_by_order[order] = [
                input_ref("image", relative_path, f"first_{order:03d}"),
                input_ref("first_image", relative_path, f"first_{order:03d}"),
            ]

        runtime_settings = merge_run_settings(project["default_run_settings"], profile.get("defaults", {}))
        runtime_settings = merge_run_settings(runtime_settings, runtime_overrides)
        runtime_settings["draft_mode"] = "i2v_first_batch"
        tasks = parse_prompt_payload(
            self._normalize_prompts_payload(prompts_text),
            seed_base=int(runtime_settings.get("seed_base", 1)),
            output_name_prefix=str(runtime_settings.get("output_name_prefix", "")),
            input_refs_by_order=input_refs_by_order,
        )
        self._apply_seed_policy(tasks, runtime_settings)

        if len(tasks) != len(images):
            raise ValueError(
                f"Uploaded {len(images)} first-frame image(s) but prompts expanded to {len(tasks)} task(s). "
                "For first-frame batch drafts the counts must match."
            )
        base_tasks = self._clone_task(tasks)
        tasks = self._expand_tasks_for_repeats(base_tasks, runtime_settings)

        manifest = {
            "id": draft_id,
            "project_id": project_id,
            "profile_id": profile_id,
            "status": "draft",
            "source_kind": "i2v_first_frame_batch",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "runtime_settings": runtime_settings,
            "source_files": {
                "prompts_json": to_relative_string(draft_dir, prompts_path),
            },
            "split_config": None,
            "base_tasks": base_tasks,
            "tasks": tasks,
            "task_count": len(tasks),
            "source_task_count": len(base_tasks),
        }
        save_json(self._draft_manifest_path(project_id, draft_id), manifest)
        return manifest

    def create_first_last_draft(
        self,
        *,
        project_id: str,
        profile_id: str,
        prompts_text: str,
        first_files: list[tuple[str, bytes]],
        last_files: list[tuple[str, bytes]] | None = None,
        continuous_pairs: bool = False,
        runtime_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        project = self.load_project(project_id)
        profile = self.load_profile(project_id, profile_id)
        if profile["input_contract"]["primary_media_kind"] != "image":
            raise ValueError("The selected workflow profile does not expect image inputs.")
        if not first_files:
            raise ValueError("First/last-frame mode needs image files.")

        draft_id = make_id("draft")
        draft_dir = self._draft_dir(project_id, draft_id)
        source_dir = ensure_dir(draft_dir / "source")
        first_dir = ensure_dir(draft_dir / "inputs" / "first")
        last_dir = ensure_dir(draft_dir / "inputs" / "last")

        prompts_path = source_dir / "prompts.json"
        save_text(prompts_path, prompts_text.strip() + "\n")

        first_images = self._write_uploaded_images(first_dir, first_files)
        pairs: list[tuple[Path, Path]] = []
        if continuous_pairs:
            if len(first_images) < 2:
                raise ValueError("Continuous first/last-frame mode needs at least two images.")
            pairs = [(first_images[index], first_images[index + 1]) for index in range(len(first_images) - 1)]
        else:
            last_images = self._write_uploaded_images(last_dir, last_files or [])
            if len(first_images) != len(last_images):
                raise ValueError(
                    f"Uploaded {len(first_images)} first-frame image(s) and {len(last_images)} last-frame image(s). "
                    "The counts must match."
                )
            pairs = list(zip(first_images, last_images))

        input_refs_by_order: dict[int, list[dict[str, Any]]] = {}
        for order, (first_path, last_path) in enumerate(pairs, start=1):
            first_relative = to_relative_string(draft_dir, first_path)
            last_relative = to_relative_string(draft_dir, last_path)
            input_refs_by_order[order] = [
                input_ref("image", first_relative, f"first_{order:03d}"),
                input_ref("first_image", first_relative, f"first_{order:03d}"),
                input_ref("last_image", last_relative, f"last_{order:03d}"),
            ]

        runtime_settings = merge_run_settings(project["default_run_settings"], profile.get("defaults", {}))
        runtime_settings = merge_run_settings(runtime_settings, runtime_overrides)
        runtime_settings["draft_mode"] = "i2v_first_last_continuous" if continuous_pairs else "i2v_first_last_batch"
        tasks = parse_prompt_payload(
            self._normalize_prompts_payload(prompts_text),
            seed_base=int(runtime_settings.get("seed_base", 1)),
            output_name_prefix=str(runtime_settings.get("output_name_prefix", "")),
            input_refs_by_order=input_refs_by_order,
        )
        self._apply_seed_policy(tasks, runtime_settings)

        if len(tasks) != len(pairs):
            raise ValueError(
                f"Built {len(pairs)} first/last-frame pair(s) but prompts expanded to {len(tasks)} task(s). "
                "The counts must match."
            )
        base_tasks = self._clone_task(tasks)
        tasks = self._expand_tasks_for_repeats(base_tasks, runtime_settings)

        manifest = {
            "id": draft_id,
            "project_id": project_id,
            "profile_id": profile_id,
            "status": "draft",
            "source_kind": "i2v_first_last_continuous" if continuous_pairs else "i2v_first_last_batch",
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "runtime_settings": runtime_settings,
            "source_files": {
                "prompts_json": to_relative_string(draft_dir, prompts_path),
            },
            "split_config": {
                "continuous_pairs": bool(continuous_pairs),
            },
            "base_tasks": base_tasks,
            "tasks": tasks,
            "task_count": len(tasks),
            "source_task_count": len(base_tasks),
        }
        save_json(self._draft_manifest_path(project_id, draft_id), manifest)
        return manifest

    def freeze_draft_to_batch(
        self,
        project_id: str,
        draft_id: str,
        *,
        status: str = "queued",
        selected_task_ids: list[str] | None = None,
        runtime_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        draft = self.load_draft(project_id, draft_id)
        batch_id = make_id("batch")
        draft_dir = self._draft_dir(project_id, draft_id)
        batch_dir = self._batch_dir(project_id, batch_id)
        ensure_dir(batch_dir)

        if (draft_dir / "inputs").exists():
            copy_tree(draft_dir / "inputs", batch_dir / "inputs")
        if (draft_dir / "source").exists():
            copy_tree(draft_dir / "source", batch_dir / "source")

        runtime_settings = self._merge_submission_runtime_settings(draft["runtime_settings"], runtime_overrides)
        selected_tasks = self._selected_tasks(draft["tasks"], selected_task_ids)
        previous_runtime = dict(draft.get("runtime_settings", {}))
        seed_keys = ("seed_mode", "seed_fixed", "seed_base")
        update_seeds = any(previous_runtime.get(key) != runtime_settings.get(key) for key in seed_keys)
        previous_repeat_count = self._repeat_count(previous_runtime)
        repeat_count = self._repeat_count(runtime_settings)
        if repeat_count != previous_repeat_count and draft.get("base_tasks"):
            selected_source_ids = {
                str(task.get("source_task_id") or task.get("task_id"))
                for task in selected_tasks
            }
            selected_base_tasks = [
                self._clone_task(task)
                for task in draft["base_tasks"]
                if str(task.get("source_task_id") or task.get("task_id")) in selected_source_ids
            ]
            if not selected_base_tasks:
                selected_base_tasks = [self._clone_task(task) for task in draft["base_tasks"]]
            self._refresh_task_runtime_fields(selected_base_tasks, runtime_settings, update_seeds=update_seeds)
            tasks = self._expand_tasks_for_repeats(selected_base_tasks, runtime_settings)
        else:
            tasks = selected_tasks
            self._refresh_task_runtime_fields(tasks, runtime_settings, update_seeds=update_seeds)
        manifest = {
            "id": batch_id,
            "project_id": project_id,
            "profile_id": draft["profile_id"],
            "draft_id": draft_id,
            "source_kind": draft["source_kind"],
            "status": status,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "latest_run_id": "",
            "schedule": None,
            "runtime_settings": runtime_settings,
            "tasks": tasks,
            "task_count": len(tasks),
            "source_task_count": int(draft.get("source_task_count") or draft["task_count"]),
            "selected_task_ids": selected_task_ids or [],
        }
        save_json(self._batch_manifest_path(project_id, batch_id), manifest)

        draft["status"] = "planned" if status == "planned" else "submitted"
        draft["latest_batch_id"] = batch_id
        self.save_draft(draft)
        return manifest

    def _selected_tasks(self, tasks: list[dict[str, Any]], selected_task_ids: list[str] | None = None) -> list[dict[str, Any]]:
        if not selected_task_ids:
            selected = tasks
        else:
            wanted = {str(item) for item in selected_task_ids if str(item).strip()}
            selected = [task for task in tasks if str(task.get("task_id", "")) in wanted]
            if not selected:
                raise ValueError("No matching tasks were selected.")
        return json.loads(json.dumps(selected, ensure_ascii=False))

    def create_run_from_batch(
        self,
        project_id: str,
        batch_id: str,
        *,
        reason: str = "new",
        selected_task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        batch = self.load_batch(project_id, batch_id)
        if reason == "planned" and batch.get("status") not in {"planned", "scheduled"}:
            raise ValueError("Only Planbox batches can be submitted from Planbox.")
        run_id = make_id("run")
        run_dir = self._run_dir(project_id, run_id)
        ensure_dir(run_dir / "outputs")

        tasks = []
        for task in self._selected_tasks(batch["tasks"], selected_task_ids):
            task_copy = json.loads(json.dumps(task, ensure_ascii=False))
            task_copy.update(
                {
                    "status": "pending",
                    "prompt_id": "",
                    "error": "",
                    "output_path": "",
                    "submitted_at": "",
                    "started_at": "",
                    "finished_at": "",
                    "wait_seconds": None,
                    "duration_seconds": None,
                    "total_seconds": None,
                }
            )
            tasks.append(task_copy)

        manifest = {
            "id": run_id,
            "project_id": project_id,
            "profile_id": batch["profile_id"],
            "batch_id": batch_id,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "started_at": "",
            "ended_at": "",
            "status": "queued",
            "reason": reason,
            "stop_requested": False,
            "recovery": {
                "state": "fresh",
                "message": "",
                "updated_at": now_iso(),
            },
            "run_settings": dict(batch["runtime_settings"]),
            "tasks": tasks,
            "selected_task_ids": selected_task_ids or [],
            "logs": [],
        }
        save_json(self._run_manifest_path(project_id, run_id), manifest)
        batch["status"] = "queued"
        batch["latest_run_id"] = run_id
        batch["latest_run_created_at"] = manifest["created_at"]
        batch["schedule"] = None
        self.save_batch(batch)
        return manifest

    def retry_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        run = self.load_run(project_id, run_id)
        return self.create_run_from_batch(project_id, run["batch_id"], reason="retry")

    def retry_run_task(self, project_id: str, run_id: str, task_id: str) -> dict[str, Any]:
        run = self.load_run(project_id, run_id)
        task_ids = {str(task.get("task_id", "")) for task in run.get("tasks", [])}
        if task_id not in task_ids:
            raise FileNotFoundError(f"Task not found in run: {task_id}")
        return self.create_run_from_batch(
            project_id,
            run["batch_id"],
            reason="task_retry",
            selected_task_ids=[task_id],
        )

    def enqueue_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        queue_state = self.load_queue_state()
        item = {"project_id": project_id, "run_id": run_id}
        if queue_state.get("current") == item:
            return queue_state
        if item not in queue_state.get("queued", []):
            queue_state.setdefault("queued", []).append(item)
        self.save_queue_state(queue_state)
        return queue_state

    def remove_queued_run(self, project_id: str, run_id: str) -> dict[str, Any]:
        queue_state = self.load_queue_state()
        queue_state["queued"] = [
            item for item in queue_state.get("queued", [])
            if not (item.get("project_id") == project_id and item.get("run_id") == run_id)
        ]
        self.save_queue_state(queue_state)
        return queue_state

    def project_detail(self, project_id: str) -> dict[str, Any]:
        project = self.load_project(project_id)
        profiles = self.list_profiles(project_id)
        drafts = self.list_drafts(project_id)
        batches = self.list_batches(project_id)
        runs = self.list_runs(project_id)

        return {
            "project": project,
            "profiles": profiles,
            "drafts": drafts,
            "batches": batches,
            "runs": runs,
        }

    def dashboard_payload(self) -> dict[str, Any]:
        projects = self.list_projects()
        queue_state = self.load_queue_state()
        return {
            "projects": projects,
            "queue_state": queue_state,
            "store_root": str(self.store_root),
        }
