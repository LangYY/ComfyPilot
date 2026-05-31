from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from ltx_batch import batch as legacy_batch

from .common import now_iso, to_relative_string
from .store import StudioStore


FINAL_RUN_STATUSES = {"completed", "failed", "stopped", "cancelled", "interrupted"}
FINAL_TASK_STATUSES = {"completed", "failed", "cancelled", "unknown", "interrupted"}
DEFAULT_DIAGNOSTIC_DOCKER_REF = "comfyui_cu128_v0812"
DIAGNOSTIC_DOCKER_REFS_BY_PORT = {
    "8189": "comfyui_cu128_v0812",
    "8191": "comfyui_ltx_rebuild_20260526",
}


class RunnerLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False

        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} acquired_at={now_iso()}\n".encode("utf-8"))
        handle.flush()
        self.handle = handle
        return True


class QueueRunner:
    def __init__(self, store: StudioStore) -> None:
        self.store = store
        self.runner_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._process_lock = RunnerLock(self.store.store_root / "runner.lock")
        self.enabled = self._process_lock.acquire()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.last_heartbeat = ""
        if self.enabled:
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
            self._thread.start()

    def enqueue_run(self, project_id: str, run_id: str) -> None:
        self.store.enqueue_run(project_id, run_id)
        self._wake_event.set()

    def wake(self) -> None:
        self._wake_event.set()

    def request_stop(self, project_id: str, run_id: str) -> dict[str, Any]:
        with self._lock:
            queue_state = self.store.load_queue_state()
            current = queue_state.get("current")
            queued = list(queue_state.get("queued", []))
            queued_match = any(
                item.get("project_id") == project_id and item.get("run_id") == run_id
                for item in queued
            )
            if queued_match:
                queue_state["queued"] = [
                    item
                    for item in queued
                    if not (item.get("project_id") == project_id and item.get("run_id") == run_id)
                ]
                self.store.save_queue_state(queue_state)
                run = self.store.load_run(project_id, run_id)
                if run.get("status") not in FINAL_RUN_STATUSES:
                    run["stop_requested"] = True
                    self._cancel_remaining_tasks(run, message="Cancelled before being submitted to ComfyUI.")
                    run["status"] = "stopped"
                    run["ended_at"] = now_iso()
                    self._append_log(run, "[STOP] Cancelled while waiting in the local submit queue.")
                    self.store.save_run(run)
                    self.store.update_batch_status(
                        project_id,
                        run["batch_id"],
                        "stopped",
                        extra={"latest_run_id": run_id},
                    )
                self._wake_event.set()
                return {
                    "running_prompt_id": "",
                    "queued_prompt_ids": [],
                    "cancelled_local_queue": True,
                    "warnings": [],
                }

        queue_state = self.store.load_queue_state()
        current = queue_state.get("current")
        if not current or current.get("project_id") != project_id or current.get("run_id") != run_id:
            run = self.store.load_run(project_id, run_id)
            if run.get("status") == "queued":
                run["stop_requested"] = True
                self._cancel_remaining_tasks(run, message="Cancelled stale queued run.")
                run["status"] = "stopped"
                run["ended_at"] = now_iso()
                self._append_log(run, "[STOP] Cancelled stale queued run.")
                self.store.save_run(run)
                self.store.update_batch_status(
                    project_id,
                    run["batch_id"],
                    "stopped",
                    extra={"latest_run_id": run_id},
                )
                return {
                    "running_prompt_id": "",
                    "queued_prompt_ids": [],
                    "cancelled_local_queue": True,
                    "warnings": ["Run was not in the local queue, but was marked stopped because its status was queued."],
                }
            raise RuntimeError("Only the current run or a run waiting in the local submit queue can be stopped.")

        run = self.store.load_run(project_id, run_id)
        project = self.store.load_project(project_id)
        run["stop_requested"] = True
        run["status"] = "stopping"
        self._append_log(run, "[STOP] Stop requested by user.")
        self.store.save_run(run)
        self.store.update_batch_status(project_id, run["batch_id"], "stopping", extra={"latest_run_id": run_id})

        running_prompt_id = ""
        queued_prompt_ids: list[str] = []
        for task in run["tasks"]:
            prompt_id = str(task.get("prompt_id", "")).strip()
            status = str(task.get("status", "")).strip().lower()
            if not prompt_id:
                continue
            if status == "running":
                running_prompt_id = prompt_id
            elif status == "queued":
                queued_prompt_ids.append(prompt_id)

        warnings: list[str] = []
        base_url = str(project["comfyui"]["base_url"]).rstrip("/")
        with requests.Session() as session:
            if running_prompt_id:
                try:
                    response = session.post(f"{base_url}/interrupt", timeout=20)
                    response.raise_for_status()
                except requests.RequestException as exc:
                    warnings.append(f"Failed to interrupt current ComfyUI job: {exc}")
            if queued_prompt_ids:
                try:
                    response = session.post(
                        f"{base_url}/queue",
                        json={"delete": queued_prompt_ids},
                        timeout=20,
                    )
                    response.raise_for_status()
                except requests.RequestException as exc:
                    warnings.append(f"Failed to delete queued ComfyUI jobs: {exc}")

        self._wake_event.set()
        return {
            "running_prompt_id": running_prompt_id,
            "queued_prompt_ids": queued_prompt_ids,
            "warnings": warnings,
        }

    def request_stop_all(self) -> dict[str, Any]:
        queue_state = self.store.load_queue_state()
        current = queue_state.get("current")
        queued = list(queue_state.get("queued", []))
        warnings: list[str] = []

        if current:
            try:
                result = self.request_stop(current["project_id"], current["run_id"])
                warnings.extend(result.get("warnings", []))
            except Exception as exc:
                warnings.append(str(exc))

        for item in queued:
            try:
                run = self.store.load_run(item["project_id"], item["run_id"])
                run["status"] = "cancelled"
                run["stop_requested"] = True
                self._cancel_remaining_tasks(run, message="Cancelled by stop all.")
                run["ended_at"] = now_iso()
                self._append_log(run, "[STOP] Cancelled by stop all before running.")
                self.store.save_run(run)
                self.store.update_batch_status(item["project_id"], run["batch_id"], "cancelled", extra={"latest_run_id": run["id"]})
            except Exception as exc:
                warnings.append(str(exc))

        queue_state = self.store.load_queue_state()
        queue_state["queued"] = []
        self.store.save_queue_state(queue_state)
        self._wake_event.set()
        return {"cancelled_queued": len(queued), "warnings": warnings}

    def _run_loop(self) -> None:
        while True:
            queue_item: dict[str, Any] | None = None
            try:
                self.last_heartbeat = now_iso()
                self._enqueue_due_scheduled_batches()
                queue_item = self._claim_next_queue_item()
                if not queue_item:
                    self._wake_event.clear()
                    self._wake_event.wait(timeout=2.0)
                    continue
                self._process_queue_item(queue_item)
            except Exception as exc:
                self.last_error = f"{now_iso()} {type(exc).__name__}: {exc}"
                try:
                    if queue_item:
                        run = self.store.load_run(queue_item["project_id"], queue_item["run_id"])
                        self._append_log(run, f"[ERROR] {exc}")
                        if run.get("status") not in FINAL_RUN_STATUSES:
                            run["status"] = "failed"
                            run["ended_at"] = now_iso()
                        self.store.save_run(run)
                        self.store.update_batch_status(
                            queue_item["project_id"],
                            run["batch_id"],
                            run["status"],
                            extra={"latest_run_id": run["id"]},
                        )
                finally:
                    if queue_item:
                        self._finish_queue_item(queue_item)
                    time.sleep(2)

    def _parse_schedule_time(self, value: str) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        for candidate in (raw, raw.replace("T", " "), raw.replace("/", "-")):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                pass
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M")
        except ValueError:
            return None

    def _enqueue_due_scheduled_batches(self) -> None:
        now = datetime.now()
        for project in self.store.list_projects():
            project_id = project["id"]
            for batch in self.store.list_batches(project_id):
                if batch.get("status") != "scheduled":
                    continue
                schedule = batch.get("schedule") or {}
                run_at = self._parse_schedule_time(str(schedule.get("run_at", "")))
                if not run_at or run_at > now:
                    continue
                try:
                    queued_batch = self.store.update_batch_status(
                        project_id,
                        batch["id"],
                        "queued",
                        extra={
                            "schedule": {
                                **schedule,
                                "status": "queued",
                                "queued_at": now_iso(),
                            }
                        },
                    )
                    run = self.store.create_run_from_batch(project_id, queued_batch["id"], reason="scheduled")
                    self.store.enqueue_run(project_id, run["id"])
                except Exception:
                    continue

    def _claim_next_queue_item(self) -> dict[str, Any] | None:
        with self._lock:
            queue_state = self.store.load_queue_state()
            current = queue_state.get("current")
            if current:
                current_runner_id = str(current.get("runner_id", "")).strip()
                if current_runner_id and current_runner_id != self.runner_id:
                    self._mark_run_interrupted(
                        current,
                        message=(
                            "Previous StudioBatch worker stopped while this run was active. "
                            "ComfyUI prompt history is not persistent after restart, so this run must be retried."
                        ),
                    )
                    queue_state["current"] = None
                    self.store.save_queue_state(queue_state)
                    current = None
                if current is None:
                    queued = list(queue_state.get("queued", []))
                    if not queued:
                        return None
                    current = queued.pop(0)
                    current["runner_id"] = self.runner_id
                    current["claimed_at"] = now_iso()
                    queue_state["current"] = current
                    queue_state["queued"] = queued
                    self.store.save_queue_state(queue_state)
                    return current
                if not current_runner_id:
                    current["runner_id"] = self.runner_id
                    current["claimed_at"] = now_iso()
                    queue_state["current"] = current
                    self.store.save_queue_state(queue_state)
                return current
            queued = list(queue_state.get("queued", []))
            if not queued:
                return None
            current = queued.pop(0)
            current["runner_id"] = self.runner_id
            current["claimed_at"] = now_iso()
            queue_state["current"] = current
            queue_state["queued"] = queued
            self.store.save_queue_state(queue_state)
            return current

    def _finish_queue_item(self, queue_item: dict[str, Any]) -> None:
        with self._lock:
            queue_state = self.store.load_queue_state()
            current = queue_state.get("current")
            if (
                current
                and current.get("project_id") == queue_item["project_id"]
                and current.get("run_id") == queue_item["run_id"]
                and str(current.get("runner_id", self.runner_id)) == self.runner_id
            ):
                queue_state["current"] = None
                self.store.save_queue_state(queue_state)
        self._wake_event.set()

    def _append_log(self, run: dict[str, Any], message: str) -> None:
        logs = list(run.get("logs", []))
        logs.append(f"{now_iso()}  {message}")
        run["logs"] = logs[-400:]

    def _mark_run_interrupted(self, queue_item: dict[str, Any], *, message: str) -> None:
        try:
            run = self.store.load_run(queue_item["project_id"], queue_item["run_id"])
        except FileNotFoundError:
            return
        if run.get("status") in FINAL_RUN_STATUSES:
            return
        for task in run.get("tasks", []):
            if task.get("status") in FINAL_TASK_STATUSES:
                continue
            task["status"] = "interrupted"
            task["error"] = message
            self._mark_task_finished(task)
        run["status"] = "interrupted"
        run["stop_requested"] = True
        run["ended_at"] = now_iso()
        run["recovery"] = {
            "state": "not_recoverable",
            "message": message,
            "updated_at": now_iso(),
        }
        self._append_log(run, f"[INTERRUPTED] {message}")
        self.store.save_run(run)
        self.store.update_batch_status(
            queue_item["project_id"],
            run["batch_id"],
            "interrupted",
            extra={"latest_run_id": run["id"]},
        )

    def _seconds_between(self, start: str, end: str) -> float | None:
        if not start or not end:
            return None
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
            end_dt = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
            return max(0.0, round((end_dt - start_dt).total_seconds(), 2))
        except ValueError:
            return None

    def _mark_task_started(self, task: dict[str, Any]) -> None:
        if not task.get("started_at"):
            task["started_at"] = now_iso()
        if task.get("submitted_at"):
            task["wait_seconds"] = self._seconds_between(str(task.get("submitted_at", "")), str(task["started_at"]))

    def _mark_task_finished(self, task: dict[str, Any]) -> None:
        task["finished_at"] = now_iso()
        task["duration_seconds"] = self._seconds_between(str(task.get("started_at", "")), str(task["finished_at"]))
        task["total_seconds"] = self._seconds_between(str(task.get("submitted_at", "")), str(task["finished_at"]))

    def _probe_prompt(self, session: requests.Session, base_url: str, prompt_id: str) -> tuple[str, dict[str, Any] | None]:
        history_response = session.get(f"{base_url}/history/{prompt_id}", timeout=60)
        history_response.raise_for_status()
        record = legacy_batch.extract_history_record(history_response.json(), prompt_id)
        if record:
            return "history", record

        queue_response = session.get(f"{base_url}/queue", timeout=60)
        queue_response.raise_for_status()
        queue_payload = queue_response.json()
        if prompt_id in json.dumps(queue_payload, ensure_ascii=False):
            return "queued", None
        return "missing", None

    def _comfy_available(self, session: requests.Session, base_url: str) -> tuple[bool, str]:
        try:
            response = session.get(f"{base_url}/queue", timeout=20)
            response.raise_for_status()
            return True, ""
        except requests.RequestException as exc:
            return False, str(exc)

    def _apply_profile_values(
        self,
        *,
        workflow: dict[str, dict[str, Any]],
        bindings: dict[str, Any],
        task: dict[str, Any],
        run_settings: dict[str, Any],
        media_values: dict[str, str],
    ) -> None:
        runtime_field_bindings = bindings.get("runtime_fields", {})
        media_inputs = bindings.get("media_inputs") or []
        if not media_inputs and bindings.get("primary_media"):
            primary_media = dict(bindings["primary_media"])
            primary_media["role"] = "first_image"
            media_inputs = [primary_media]
        for media_binding in media_inputs:
            role = str(media_binding.get("role", "first_image"))
            value = media_values.get(role) or media_values.get("image")
            if not value:
                continue
            media_node = legacy_batch.ensure_node(workflow, media_binding, role)
            media_node.setdefault("inputs", {})[media_binding.get("input_name", "image")] = value
            upload_input_name = media_binding.get("upload_input_name")
            if upload_input_name:
                media_node["inputs"][upload_input_name] = media_binding.get("upload_value", "image")

        positive_prompt = bindings["positive_prompt"]
        positive_node = legacy_batch.ensure_node(workflow, positive_prompt, "positive prompt")
        positive_node.setdefault("inputs", {})[positive_prompt["input_name"]] = task["prompt_text"]

        draft_mode = str(run_settings.get("draft_mode") or "").strip().lower()
        if draft_mode:
            text_to_video_enabled = draft_mode in {"t2v", "text_to_video", "text-to-video", "prompt_only"}
            t2v_switches = runtime_field_bindings.get("text_to_video_enabled", [])
            if not t2v_switches:
                t2v_switches = self._detect_text_to_video_switch_bindings(workflow)
            for binding in t2v_switches:
                switch_node = legacy_batch.ensure_node(workflow, binding, "text-to-video switch")
                switch_node.setdefault("inputs", {})[binding["input_name"]] = bool(text_to_video_enabled)

        negative_prompt = bindings.get("negative_prompt")
        negative_text = str(run_settings.get("negative_prompt_text", "")).strip()
        if negative_prompt and negative_text:
            negative_node = legacy_batch.ensure_node(workflow, negative_prompt, "negative prompt")
            negative_node.setdefault("inputs", {})[negative_prompt["input_name"]] = negative_text

        width_value = run_settings.get("width_pixels")
        if width_value not in (None, ""):
            for binding in runtime_field_bindings.get("width_pixels", []):
                width_node = legacy_batch.ensure_node(workflow, binding, "output width")
                width_node.setdefault("inputs", {})[binding["input_name"]] = int(width_value)

        height_value = run_settings.get("height_pixels")
        if height_value not in (None, ""):
            for binding in runtime_field_bindings.get("height_pixels", []):
                height_node = legacy_batch.ensure_node(workflow, binding, "output height")
                height_node.setdefault("inputs", {})[binding["input_name"]] = int(height_value)

        duration_value = run_settings.get("duration_seconds")
        if duration_value not in (None, ""):
            duration_bindings = runtime_field_bindings.get("duration_seconds", [])
            if not duration_bindings:
                duration_bindings = self._detect_duration_bindings(workflow)
            for binding in duration_bindings:
                duration_node = legacy_batch.ensure_node(workflow, binding, "generation duration")
                duration_node.setdefault("inputs", {})[binding["input_name"]] = int(duration_value)

        save_video = bindings["save_video"]
        save_node = legacy_batch.ensure_node(workflow, save_video, "save video")
        output_name = str(task["expected_output_name"])
        prefix_root = str(run_settings.get("save_prefix_root", "batch_studio_v2")).strip("/\\")
        prefix_name = legacy_batch.sanitize_prefix(output_name)
        final_prefix = f"{prefix_root}/{prefix_name}" if prefix_root else prefix_name
        save_node.setdefault("inputs", {})[save_video["input_name"]] = final_prefix

        for seed_binding in bindings.get("seed_nodes", []):
            seed_node = legacy_batch.ensure_node(workflow, seed_binding, "seed")
            seed_node.setdefault("inputs", {})[seed_binding["input_name"]] = int(task["seed_value"])

    def _detect_duration_bindings(self, workflow: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        aliases = {"duration", "duration_seconds", "seconds", "length", "video_length", "num_seconds"}
        title_aliases = ("duration", "seconds", "时长", "秒")
        bindings: list[dict[str, Any]] = []
        for node_id, node in workflow.items():
            inputs = node.get("inputs", {})
            title_blob = f"{legacy_batch.node_title(node)} {node_id} {node.get('class_type', '')}".lower()
            for input_name, value in inputs.items():
                input_key = str(input_name).lower()
                input_matches = input_key in aliases
                title_matches = input_key == "value" and any(alias in title_blob for alias in title_aliases)
                if not input_matches and not title_matches:
                    continue
                if not isinstance(value, (int, float)):
                    continue
                bindings.append(
                    {
                        "id": str(node_id),
                        "input_name": str(input_name),
                        "current_value": value,
                        "class_type": str(node.get("class_type", "")),
                        "title": legacy_batch.node_title(node),
                    }
                )
        return bindings

    def _detect_text_to_video_switch_bindings(self, workflow: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        bindings: list[dict[str, Any]] = []
        title_aliases = ("text to video", "text-to-video", "text2video", "t2v", "文生视频", "文本到视频")
        for node_id, node in workflow.items():
            inputs = node.get("inputs", {})
            title_blob = f"{legacy_batch.node_title(node)} {node_id} {node.get('class_type', '')}".lower()
            if not any(alias in title_blob for alias in title_aliases):
                continue
            for input_name, value in inputs.items():
                if not isinstance(value, bool):
                    continue
                bindings.append(
                    {
                        "id": str(node_id),
                        "input_name": str(input_name),
                        "current_value": value,
                        "class_type": str(node.get("class_type", "")),
                        "title": legacy_batch.node_title(node),
                    }
                )
        return bindings

    def _free_comfyui_memory(
        self,
        session: requests.Session,
        base_url: str,
        *,
        unload_models: bool = False,
        free_memory: bool = True,
    ) -> str:
        response = session.post(
            f"{base_url.rstrip('/')}/free",
            json={"unload_models": unload_models, "free_memory": free_memory},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ComfyUI /free failed ({response.status_code}): {response.text}")
        return response.text.strip()

    def _perform_maintenance_if_needed(
        self,
        run: dict[str, Any],
        session: requests.Session,
        base_url: str,
        run_settings: dict[str, Any],
        completed_count: int,
    ) -> bool:
        interval = int(run_settings.get("maintenance_interval_tasks") or 0)
        if interval <= 0 or completed_count <= 0 or completed_count % interval != 0:
            return False
        if run.get("stop_requested"):
            return False

        mode = str(run_settings.get("maintenance_memory_mode") or "free_memory").strip().lower()
        unload_models = mode in {"unload_models", "unload", "full"}
        free_memory = mode in {"free_memory", "unload_models", "unload", "full", "light"}
        if free_memory:
            try:
                result_text = self._free_comfyui_memory(
                    session=session,
                    base_url=base_url,
                    unload_models=unload_models,
                    free_memory=True,
                )
                self._append_log(
                    run,
                    f"[MAINT] task_count={completed_count} free_memory=true unload_models={str(unload_models).lower()} result={result_text or 'ok'}",
                )
            except Exception as exc:
                self._append_log(run, f"[MAINT] ComfyUI memory release failed: {exc}")

        cooldown_seconds = float(run_settings.get("maintenance_cooldown_seconds") or 0)
        if cooldown_seconds > 0 and not run.get("stop_requested"):
            self._append_log(run, f"[MAINT] Cooling down {cooldown_seconds:g}s after {completed_count} completed tasks.")
            self.store.save_run(run)
            time.sleep(cooldown_seconds)
        return True

    def _command_text(self, command: list[str], *, timeout: int = 10) -> tuple[bool, str]:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except Exception as exc:
            return False, str(exc)
        output = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)
        return completed.returncode == 0, output

    def _gpu_snapshot(self) -> dict[str, Any]:
        fields = [
            "timestamp",
            "name",
            "driver_version",
            "pstate",
            "memory.total",
            "memory.used",
            "memory.free",
            "temperature.gpu",
            "power.draw",
            "utilization.gpu",
        ]
        ok, output = self._command_text(
            [
                "nvidia-smi",
                f"--query-gpu={','.join(fields)}",
                "--format=csv,noheader,nounits",
            ],
            timeout=8,
        )
        if not ok:
            return {"available": False, "error": output}
        rows: list[dict[str, Any]] = []
        for line in output.splitlines():
            values = [item.strip() for item in line.split(",")]
            if len(values) != len(fields):
                continue
            rows.append(dict(zip(fields, values)))
        return {"available": True, "gpus": rows}

    def _diagnostic_docker_ref(self, base_url: str | None = None) -> str:
        configured = os.environ.get("COMFYPILOT_DIAG_DOCKER_REF", "").strip()
        if configured:
            return configured
        match = re.search(r":(\d+)(?:/|$)", str(base_url or ""))
        if match:
            return DIAGNOSTIC_DOCKER_REFS_BY_PORT.get(match.group(1), DEFAULT_DIAGNOSTIC_DOCKER_REF)
        return DEFAULT_DIAGNOSTIC_DOCKER_REF

    def _docker_snapshot(self, base_url: str | None = None) -> dict[str, Any]:
        ref = self._diagnostic_docker_ref(base_url)
        ok, output = self._command_text(["docker", "inspect", ref], timeout=8)
        if not ok:
            return {"available": False, "ref": ref, "error": output}
        try:
            payload = json.loads(output)[0]
        except Exception as exc:
            return {"available": False, "ref": ref, "error": f"docker inspect parse failed: {exc}"}
        state = payload.get("State", {})
        host_config = payload.get("HostConfig", {})
        return {
            "available": True,
            "ref": ref,
            "id": payload.get("Id", ""),
            "name": payload.get("Name", ""),
            "image": payload.get("Config", {}).get("Image", ""),
            "state": {
                "status": state.get("Status"),
                "running": state.get("Running"),
                "oom_killed": state.get("OOMKilled"),
                "exit_code": state.get("ExitCode"),
                "started_at": state.get("StartedAt"),
                "finished_at": state.get("FinishedAt"),
                "error": state.get("Error"),
            },
            "limits": {
                "memory": host_config.get("Memory"),
                "memory_swap": host_config.get("MemorySwap"),
                "nano_cpus": host_config.get("NanoCpus"),
                "shm_size": host_config.get("ShmSize"),
            },
            "mounts": [
                {
                    "source": item.get("Source"),
                    "destination": item.get("Destination"),
                    "type": item.get("Type"),
                }
                for item in payload.get("Mounts", [])
            ],
        }

    def _media_diagnostics(self, paths_by_kind: dict[str, Path]) -> dict[str, Any]:
        diagnostics: dict[str, Any] = {}
        for kind, path in paths_by_kind.items():
            entry: dict[str, Any] = {
                "path": str(path),
                "exists": path.exists(),
            }
            if path.exists():
                entry["bytes"] = path.stat().st_size
                try:
                    from PIL import Image

                    with Image.open(path) as image:
                        entry["image_size"] = list(image.size)
                        entry["image_mode"] = image.mode
                except Exception as exc:
                    entry["image_error"] = str(exc)
            diagnostics[kind] = entry
        return diagnostics

    def _runtime_field_values(
        self,
        workflow: dict[str, dict[str, Any]],
        bindings: dict[str, Any],
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        runtime_fields = dict(bindings.get("runtime_fields", {}))
        if "duration_seconds" not in runtime_fields:
            runtime_fields["duration_seconds"] = self._detect_duration_bindings(workflow)
        for key, field_bindings in runtime_fields.items():
            values[key] = []
            for binding in field_bindings or []:
                node = workflow.get(str(binding.get("id", "")), {})
                values[key].append(
                    {
                        "id": binding.get("id"),
                        "input_name": binding.get("input_name"),
                        "value": node.get("inputs", {}).get(binding.get("input_name")),
                        "class_type": node.get("class_type"),
                        "title": legacy_batch.node_title(node) if node else binding.get("title", ""),
                    }
                )
        return values

    def _write_task_preflight_diagnostics(
        self,
        *,
        run_dir: Path,
        project: dict[str, Any],
        profile: dict[str, Any],
        batch: dict[str, Any],
        run: dict[str, Any],
        task: dict[str, Any],
        run_settings: dict[str, Any],
        workflow: dict[str, dict[str, Any]],
        bindings: dict[str, Any],
        paths_by_kind: dict[str, Path],
        media_values: dict[str, str],
    ) -> Path:
        diagnostics_dir = run_dir / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        path = diagnostics_dir / f"task_{int(task.get('order', 0)):03d}_preflight.json"
        payload = {
            "created_at": now_iso(),
            "runner_id": self.runner_id,
            "project": {
                "id": project.get("id"),
                "name": project.get("name"),
                "comfyui_base_url": project.get("comfyui", {}).get("base_url"),
            },
            "profile": {
                "id": profile.get("id"),
                "name": profile.get("name"),
                "bindings": profile.get("bindings", {}),
            },
            "batch": {
                "id": batch.get("id"),
                "source_kind": batch.get("source_kind"),
                "task_count": batch.get("task_count"),
            },
            "run": {
                "id": run.get("id"),
                "status": run.get("status"),
                "reason": run.get("reason"),
            },
            "task": {
                "task_id": task.get("task_id"),
                "order": task.get("order"),
                "source_index": task.get("source_index"),
                "expected_output_name": task.get("expected_output_name"),
                "seed_value": task.get("seed_value"),
                "prompt_chars": len(str(task.get("prompt_text", ""))),
                "prompt_preview": str(task.get("prompt_text", ""))[:600],
                "input_refs": task.get("input_refs", []),
            },
            "run_settings": run_settings,
            "media_paths": self._media_diagnostics(paths_by_kind),
            "media_values": media_values,
            "workflow_runtime_values": self._runtime_field_values(workflow, bindings),
            "gpu": self._gpu_snapshot(),
            "docker": self._docker_snapshot(project.get("comfyui", {}).get("base_url")),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _append_task_sample(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()

    def _start_task_diagnostic_sampler(
        self,
        *,
        run_dir: Path,
        run: dict[str, Any],
        task: dict[str, Any],
        base_url: str,
        interval_seconds: float,
    ) -> tuple[threading.Event, threading.Thread, Path]:
        diagnostics_dir = run_dir / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        path = diagnostics_dir / f"task_{int(task.get('order', 0)):03d}_samples.jsonl"
        stop_event = threading.Event()
        interval = max(5.0, min(float(interval_seconds or 10), 30.0))

        def sample_loop() -> None:
            while not stop_event.is_set():
                self._append_task_sample(
                    path,
                    {
                        "created_at": now_iso(),
                        "runner_id": self.runner_id,
                        "run_id": run.get("id"),
                        "task_id": task.get("task_id"),
                        "task_order": task.get("order"),
                        "task_status": task.get("status"),
                        "prompt_id": task.get("prompt_id"),
                        "gpu": self._gpu_snapshot(),
                        "docker": self._docker_snapshot(base_url),
                    },
                )
                stop_event.wait(interval)

        thread = threading.Thread(target=sample_loop, daemon=True)
        thread.start()
        return stop_event, thread, path

    def _stop_task_diagnostic_sampler(self, stop_event: threading.Event, thread: threading.Thread) -> None:
        stop_event.set()
        thread.join(timeout=2)

    def _cancel_remaining_tasks(self, run: dict[str, Any], *, message: str) -> None:
        for task in run["tasks"]:
            if task.get("status") in FINAL_TASK_STATUSES:
                continue
            task["status"] = "cancelled"
            task["error"] = message
            self._mark_task_finished(task)

    def _resolve_input_path(self, batch_dir: Path, task: dict[str, Any], expected_kind: str) -> Path | None:
        for ref in task.get("input_refs", []):
            if ref.get("kind") == expected_kind:
                return batch_dir / ref["path"]
        return None

    def _resolve_input_paths(self, batch_dir: Path, task: dict[str, Any]) -> dict[str, Path]:
        resolved: dict[str, Path] = {}
        for ref in task.get("input_refs", []):
            kind = str(ref.get("kind", ""))
            if kind and kind not in resolved:
                resolved[kind] = batch_dir / ref["path"]
        return resolved

    def _upload_media_values(
        self,
        *,
        session: requests.Session,
        base_url: str,
        run_settings: dict[str, Any],
        paths_by_kind: dict[str, Path],
    ) -> dict[str, str]:
        uploaded_by_path: dict[str, str] = {}
        media_values: dict[str, str] = {}
        for kind, input_path in paths_by_kind.items():
            if not input_path.exists():
                raise FileNotFoundError(f"Image input not found: {input_path}")
            cache_key = str(input_path.resolve())
            if cache_key not in uploaded_by_path:
                if bool(run_settings.get("upload_files", True)):
                    upload_result = legacy_batch.upload_image(
                        session,
                        base_url,
                        input_path,
                        str(run_settings.get("upload_subfolder", "")),
                    )
                    uploaded_by_path[cache_key] = legacy_batch.uploaded_image_value(upload_result)
                else:
                    uploaded_by_path[cache_key] = str(input_path.resolve())
            media_values[kind] = uploaded_by_path[cache_key]
        if "image" not in media_values and "first_image" in media_values:
            media_values["image"] = media_values["first_image"]
        return media_values

    def _download_video_from_comfyui(
        self,
        *,
        session: requests.Session,
        base_url: str,
        record: dict[str, Any],
        destination: Path,
        overwrite: bool,
    ) -> Path:
        entry = legacy_batch.pick_video_entry(record)
        if str(entry.get("type", "")) != "output":
            raise RuntimeError(
                f"Expected an output video file, got type={entry.get('type')}. "
                "Set the workflow save node to write to ComfyUI output."
            )
        if destination.exists() and not overwrite:
            raise FileExistsError(f"Output already exists: {destination}.")

        destination.parent.mkdir(parents=True, exist_ok=True)
        response = session.get(
            f"{base_url}/view",
            params={
                "filename": str(entry.get("filename", "")),
                "subfolder": str(entry.get("subfolder", "")),
                "type": str(entry.get("type", "output")),
            },
            timeout=300,
        )
        response.raise_for_status()
        destination.write_bytes(response.content)
        return destination

    def _save_completed_video(
        self,
        *,
        session: requests.Session,
        base_url: str,
        record: dict[str, Any],
        comfyui_output_dir: Path,
        run_outputs_dir: Path,
        final_output_dir: str,
        output_name: str,
        overwrite: bool,
    ) -> tuple[Path, Path | None]:
        run_outputs_dir.mkdir(parents=True, exist_ok=True)
        run_copy_path = run_outputs_dir / output_name
        try:
            saved_path = self._download_video_from_comfyui(
                session=session,
                base_url=base_url,
                record=record,
                destination=run_copy_path,
                overwrite=True,
            )
        except Exception:
            if not str(comfyui_output_dir).strip() or str(comfyui_output_dir) == ".":
                raise
            saved_path = legacy_batch.copy_video_to_project(
                record=record,
                comfyui_output_dir=comfyui_output_dir,
                outputs_dir=run_outputs_dir,
                output_name=output_name,
                overwrite=True,
            )

        final_dir = str(final_output_dir or "").strip()
        if not final_dir:
            return saved_path, None

        final_path = Path(final_dir) / output_name
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.resolve() != saved_path.resolve():
            if final_path.exists() and not overwrite:
                raise FileExistsError(f"Final output already exists: {final_path}.")
            shutil.copy2(saved_path, final_path)
        return saved_path, final_path

    def _mark_unknown_submitted_tasks(
        self,
        *,
        run: dict[str, Any],
        session: requests.Session,
        base_url: str,
    ) -> None:
        for task in run["tasks"]:
            if not task.get("prompt_id") or task.get("status") not in {"queued", "running"}:
                continue
            state, record = self._probe_prompt(session, base_url, str(task["prompt_id"]))
            if state == "missing":
                task["status"] = "unknown"
                task["error"] = "Prompt was not present in ComfyUI queue/history during recovery."
                self._mark_task_finished(task)
                self._append_log(
                    run,
                    f"[UNKNOWN] prompt_id={task['prompt_id']} missing during recovery for task {task['order']}.",
                )
            elif state == "history" and record:
                status = record.get("status", {})
                if status.get("completed") or record.get("outputs"):
                    try:
                        task["recovered_record"] = record
                    except Exception:
                        pass

    def _process_queue_item(self, queue_item: dict[str, Any]) -> None:
        project_id = queue_item["project_id"]
        run_id = queue_item["run_id"]
        run = self.store.load_run(project_id, run_id)
        if run.get("status") in FINAL_RUN_STATUSES:
            self._finish_queue_item(queue_item)
            return

        project = self.store.load_project(project_id)
        profile = self.store.load_profile(project_id, run["profile_id"])
        batch = self.store.load_batch(project_id, run["batch_id"])
        batch_dir = self.store._batch_dir(project_id, batch["id"])
        run_dir = self.store._run_dir(project_id, run_id)
        outputs_dir = run_dir / "outputs"
        compiled_workflow = self.store.load_compiled_profile_workflow(project_id, run["profile_id"])
        bindings = profile["bindings"]

        run_settings = dict(run.get("run_settings", {}))
        base_url = str(project["comfyui"]["base_url"]).rstrip("/")
        comfy_output_dir_raw = str(project["comfyui"].get("output_dir", "")).strip()
        comfy_output_dir = Path(comfy_output_dir_raw) if comfy_output_dir_raw else Path()
        final_output_dir = str(run_settings.get("final_output_dir", "")).strip()

        resuming = any(str(task.get("prompt_id", "")).strip() for task in run["tasks"])
        if resuming:
            self._mark_run_interrupted(
                queue_item,
                message=(
                    "This run already had ComfyUI prompt ids before the worker started processing it. "
                    "ComfyUI prompt history is not persistent after restart, so rerun the batch to resubmit all tasks."
                ),
            )
            self._finish_queue_item(queue_item)
            return
        if not run.get("started_at"):
            run["started_at"] = now_iso()
        if run.get("stop_requested"):
            run["status"] = "stopping"
        else:
            run["status"] = "running"
        self.store.update_batch_status(project_id, batch["id"], run["status"], extra={"latest_run_id": run_id})
        run["recovery"] = {
            "state": "fresh",
            "message": "",
            "updated_at": now_iso(),
        }
        self._append_log(run, f"[RUN] Processing run {run_id} for batch {batch['id']}.")
        self.store.save_run(run)

        with requests.Session() as session:
            available, error_message = self._comfy_available(session, base_url)
            if not available:
                run["status"] = "queued"
                run["recovery"] = {
                    "state": "waiting_for_comfyui",
                    "message": error_message,
                    "updated_at": now_iso(),
                }
                self._append_log(run, f"[WAIT] ComfyUI unavailable: {error_message}")
                self.store.save_run(run)
                time.sleep(5)
                return

            for task in run["tasks"]:
                if run.get("stop_requested"):
                    self._cancel_remaining_tasks(run, message="Stopped by user.")
                    run["status"] = "stopped"
                    break
                if task.get("status") in FINAL_TASK_STATUSES:
                    continue
                if str(task.get("prompt_id", "")).strip():
                    continue

                try:
                    workflow = copy.deepcopy(compiled_workflow)
                    paths_by_kind = self._resolve_input_paths(batch_dir, task)
                    media_values = self._upload_media_values(
                        session=session,
                        base_url=base_url,
                        run_settings=run_settings,
                        paths_by_kind=paths_by_kind,
                    )
                    self._apply_profile_values(
                        workflow=workflow,
                        bindings=bindings,
                        task=task,
                        run_settings=run_settings,
                        media_values=media_values,
                    )
                    preflight_path = self._write_task_preflight_diagnostics(
                        run_dir=run_dir,
                        project=project,
                        profile=profile,
                        batch=batch,
                        run=run,
                        task=task,
                        run_settings=run_settings,
                        workflow=workflow,
                        bindings=bindings,
                        paths_by_kind=paths_by_kind,
                        media_values=media_values,
                    )
                    task.setdefault("diagnostics", {})["preflight"] = to_relative_string(run_dir, preflight_path)
                    self._append_log(
                        run,
                        f"[DIAG] task={task['order']} preflight={task['diagnostics']['preflight']}",
                    )
                    prompt_id = legacy_batch.submit_prompt(session, base_url, workflow)
                    task["prompt_id"] = prompt_id
                    task["status"] = "queued"
                    task["submitted_at"] = now_iso()
                    self._append_log(
                        run,
                        f"[QUEUE] task={task['order']} prompt_id={prompt_id} output={task['expected_output_name']}",
                    )
                    self.store.save_run(run)
                except Exception as exc:
                    task["status"] = "failed"
                    task["error"] = str(exc)
                    self._mark_task_finished(task)
                    self._append_log(run, f"[FAIL] task={task['order']} submit error={exc}")
                    self.store.save_run(run)
                    continue

                prompt_id = str(task.get("prompt_id", "")).strip()
                task["status"] = "running"
                self._mark_task_started(task)
                maintenance_performed = False
                sampler_stop, sampler_thread, sampler_path = self._start_task_diagnostic_sampler(
                    run_dir=run_dir,
                    run=run,
                    task=task,
                    base_url=base_url,
                    interval_seconds=float(run_settings.get("poll_interval_seconds", 5)),
                )
                task.setdefault("diagnostics", {})["samples"] = to_relative_string(run_dir, sampler_path)
                self._append_log(
                    run,
                    f"[DIAG] task={task['order']} samples={task['diagnostics']['samples']}",
                )
                self.store.save_run(run)

                cached_record = task.pop("recovered_record", None)
                try:
                    if cached_record:
                        record = cached_record
                    else:
                        record = legacy_batch.wait_for_completion(
                            session=session,
                            base_url=base_url,
                            prompt_id=prompt_id,
                            poll_interval_seconds=float(run_settings.get("poll_interval_seconds", 5)),
                            timeout_seconds=int(run_settings.get("timeout_seconds", 3600)),
                            is_cancelled_callback=lambda: bool(self.store.load_run(project_id, run_id).get("stop_requested")),
                        )
                    saved_path, final_path = self._save_completed_video(
                        session=session,
                        base_url=base_url,
                        record=record,
                        comfyui_output_dir=comfy_output_dir,
                        run_outputs_dir=outputs_dir,
                        final_output_dir=final_output_dir,
                        output_name=str(task["expected_output_name"]),
                        overwrite=True,
                    )
                    task["status"] = "completed"
                    task["output_path"] = to_relative_string(run_dir, saved_path)
                    task["final_output_path"] = str(final_path) if final_path else ""
                    self._mark_task_finished(task)
                    self._append_log(
                        run,
                        f"[DONE] task={task['order']} prompt_id={prompt_id} output={final_path or saved_path}",
                    )
                    self.store.save_run(run)
                    completed_count = sum(1 for item in run["tasks"] if item.get("status") == "completed")
                    maintenance_performed = self._perform_maintenance_if_needed(
                        run=run,
                        session=session,
                        base_url=base_url,
                        run_settings=run_settings,
                        completed_count=completed_count,
                    )
                except legacy_batch.BatchCancelled:
                    run["stop_requested"] = True
                    self._cancel_remaining_tasks(run, message="Stopped by user.")
                    run["status"] = "stopped"
                    self._append_log(run, f"[STOP] task={task['order']} prompt_id={prompt_id}")
                    self.store.save_run(run)
                    break
                except Exception as exc:
                    task["status"] = "failed"
                    task["error"] = str(exc)
                    self._mark_task_finished(task)
                    self._append_log(
                        run,
                        f"[FAIL] task={task['order']} prompt_id={prompt_id} wait error={exc}",
                    )
                    self.store.save_run(run)
                finally:
                    self._stop_task_diagnostic_sampler(sampler_stop, sampler_thread)

                cooldown_seconds = float(run_settings.get("task_cooldown_seconds", 10) or 0)
                if cooldown_seconds > 0 and not run.get("stop_requested") and not maintenance_performed:
                    self._append_log(run, f"[COOLDOWN] Waiting {cooldown_seconds:g}s before next task.")
                    self.store.save_run(run)
                    time.sleep(cooldown_seconds)

        task_statuses = [str(task.get("status", "")) for task in run["tasks"]]
        if run.get("stop_requested") or "cancelled" in task_statuses:
            run["status"] = "stopped"
        elif any(status in {"failed", "unknown"} for status in task_statuses):
            run["status"] = "failed"
        elif all(status == "completed" for status in task_statuses):
            run["status"] = "completed"
        else:
            run["status"] = "failed"
        run["ended_at"] = now_iso()
        self.store.save_run(run)
        self.store.update_batch_status(project_id, batch["id"], run["status"], extra={"latest_run_id": run_id})
        self._finish_queue_item(queue_item)
