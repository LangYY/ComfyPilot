from __future__ import annotations

import argparse
import copy
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ltx_batch.batch import BatchRunOptions, load_prompts, run_batch, validate_workflow_config
from ltx_batch.project import load_json, load_text, normalize_config, project_root, save_json, save_text
from ltx_batch.storyboard import StoryboardCell, split_storyboard


ROOT = project_root()
CONFIG_PATH = ROOT / "config" / "workflow_config.json"
PROMPTS_PATH = ROOT / "data" / "prompts.json"
WORKFLOW_PATH = ROOT / "workflows" / "ltx_i2v_api.json"
FAILED_JOBS_PATH = ROOT / "failed_jobs.json"
STORYBOARD_PATH = ROOT / "storyboard_3x4.png"
CELLS_DIR = ROOT / "cells"
OUTPUTS_DIR = ROOT / "outputs"
WEB_DIR = ROOT / "web"


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_json_text(text: str, label: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{label} JSON 解析失败: {exc}") from exc


def list_cells() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(CELLS_DIR.glob("*.png")):
        items.append(
            {
                "name": path.name,
                "index": int(path.stem),
                "url": f"/cells/{path.name}?ts={int(path.stat().st_mtime)}",
            }
        )
    return items


def list_outputs() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(OUTPUTS_DIR.iterdir()):
        if path.is_file() and path.name != ".gitkeep":
            stat = path.stat()
            items.append(
                {
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                    "url": f"/outputs/{path.name}?ts={int(stat.st_mtime)}",
                }
            )
    return items


def serialize_split_results(results: list[StoryboardCell]) -> list[dict[str, Any]]:
    return [
        {
            "index": item.index,
            "row": item.row,
            "col": item.col,
            "crop_box": list(item.crop_box),
            "name": item.output_path.name,
            "url": f"/cells/{item.output_path.name}?ts={int(item.output_path.stat().st_mtime)}",
        }
        for item in results
    ]


class BatchManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = self._initial_state()

    def _initial_state(self) -> dict[str, Any]:
        return {
            "running": False,
            "phase": "idle",
            "started_at": None,
            "ended_at": None,
            "current_index": None,
            "current_output_name": None,
            "stop_requested": False,
            "progress": {
                "total": 0,
                "queued": 0,
                "running": 0,
                "completed": 0,
                "failed": 0,
                "skipped": 0,
                "cancelled": 0,
            },
            "bindings": None,
            "last_summary": None,
            "last_error": None,
            "logs": [],
            "items": [],
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = copy.deepcopy(self._state)
        state["logs"] = list(state["logs"])
        return state

    def _append_log(self, message: str) -> None:
        with self._lock:
            logs = deque(self._state["logs"], maxlen=400)
            logs.append(f"{now_iso()}  {message}")
            self._state["logs"] = list(logs)

    def _recompute_progress_locked(self) -> None:
        total = int(self._state["progress"].get("total", 0))
        counts = {
            "queued": 0,
            "running": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "cancelled": 0,
        }
        for item in self._state["items"]:
            status = str(item.get("status", "")).strip().lower()
            if status in counts:
                counts[status] += 1

        self._state["progress"] = {
            "total": total,
            **counts,
        }

    def _handle_progress(self, payload: dict[str, Any]) -> None:
        event = payload.get("event")
        with self._lock:
            if event == "initialized":
                self._state["phase"] = "ready"
                self._state["bindings"] = payload.get("bindings")
                self._state["progress"]["total"] = int(payload.get("total", 0))
                self._recompute_progress_locked()
            elif event == "job_started":
                self._state["phase"] = "queueing"
                self._state["current_index"] = payload.get("index")
                self._state["current_output_name"] = payload.get("output_name")
                self._state["items"].append(
                    {
                        "index": payload.get("index"),
                        "output_name": payload.get("output_name"),
                        "status": "preparing",
                        "prompt_id": None,
                        "error": None,
                    }
                )
                self._recompute_progress_locked()
            elif event == "job_submitted":
                for item in reversed(self._state["items"]):
                    if item["index"] == payload.get("index"):
                        item["prompt_id"] = payload.get("prompt_id")
                        item["status"] = "queued"
                        break
                self._recompute_progress_locked()
            elif event == "job_running":
                self._state["phase"] = "running"
                self._state["current_index"] = payload.get("index")
                self._state["current_output_name"] = payload.get("output_name")
                for item in reversed(self._state["items"]):
                    if item["index"] == payload.get("index"):
                        item["status"] = "running"
                        break
                self._recompute_progress_locked()
            elif event == "job_finished":
                status = payload.get("status")
                for item in reversed(self._state["items"]):
                    if item["index"] == payload.get("index"):
                        item["status"] = status
                        item["error"] = payload.get("error")
                        item["saved_path"] = payload.get("saved_path")
                        break
                if self._state["current_index"] == payload.get("index"):
                    self._state["current_index"] = None
                    self._state["current_output_name"] = None
                self._recompute_progress_locked()
            elif event == "complete":
                self._state["phase"] = "stopped" if payload.get("stopped") else "finished"
                self._state["last_summary"] = payload.get("summary")
                self._state["current_index"] = None
                self._state["current_output_name"] = None
                self._recompute_progress_locked()

    def start(self, options: BatchRunOptions) -> None:
        with self._lock:
            if self._state["running"]:
                raise RuntimeError("当前已有批处理正在运行，请等待完成。")
            self._state = self._initial_state()
            self._state["running"] = True
            self._state["phase"] = "starting"
            self._state["started_at"] = now_iso()

        self._thread = threading.Thread(
            target=self._run_worker,
            args=(options,),
            daemon=True,
        )
        self._thread.start()

    def _run_worker(self, options: BatchRunOptions) -> None:
        try:
            summary = run_batch(
                options=options,
                on_log=self._append_log,
                on_progress=self._handle_progress,
                is_cancelled_callback=self.is_stop_requested,
            )
            with self._lock:
                self._state["last_summary"] = summary.to_dict()
        except Exception as exc:
            self._append_log(f"[ERROR] {exc}")
            with self._lock:
                self._state["phase"] = "error"
                self._state["last_error"] = str(exc)
        finally:
            with self._lock:
                self._state["running"] = False
                self._state["ended_at"] = now_iso()

    def is_stop_requested(self) -> bool:
        with self._lock:
            return bool(self._state.get("stop_requested"))

    def request_stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._state["running"]:
                raise RuntimeError("当前没有正在运行的批处理任务。")
            self._state["stop_requested"] = True
            self._state["phase"] = "stopping"

            queued_prompt_ids: list[str] = []
            running_prompt_id: str | None = None
            for item in self._state["items"]:
                prompt_id = str(item.get("prompt_id") or "").strip()
                status = str(item.get("status") or "").strip().lower()
                if not prompt_id:
                    continue
                if status == "running":
                    running_prompt_id = prompt_id
                elif status == "queued":
                    queued_prompt_ids.append(prompt_id)

        return {
            "running_prompt_id": running_prompt_id,
            "queued_prompt_ids": queued_prompt_ids,
        }


batch_manager = BatchManager()

app = FastAPI(title="LTX Storyboard Batch Console")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")
app.mount("/cells", StaticFiles(directory=CELLS_DIR), name="cells")
app.mount("/outputs", StaticFiles(directory=OUTPUTS_DIR), name="outputs")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    failed_jobs = load_json(FAILED_JOBS_PATH) if FAILED_JOBS_PATH.exists() else []
    config = normalize_config(load_json(CONFIG_PATH))
    return {
        "project_root": str(ROOT),
        "storyboard_exists": STORYBOARD_PATH.exists(),
        "config": config,
        "prompts_text": load_text(PROMPTS_PATH),
        "cells": list_cells(),
        "outputs": list_outputs(),
        "failed_jobs": failed_jobs,
        "batch_state": batch_manager.snapshot(),
        "paths": {
            "config": str(CONFIG_PATH),
            "prompts": str(PROMPTS_PATH),
            "workflow": str(WORKFLOW_PATH),
            "storyboard": str(STORYBOARD_PATH),
        },
    }


@app.put("/api/config")
def save_config(payload: dict[str, Any]) -> dict[str, Any]:
    config = normalize_config(payload)
    save_json(CONFIG_PATH, config)
    return {"ok": True, "config": config}


@app.put("/api/prompts")
def save_prompts(payload: dict[str, str]) -> dict[str, Any]:
    text = payload.get("text", "")
    parsed = ensure_json_text(text, "prompts")
    try:
        count = len(load_prompts(parsed))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    save_text(PROMPTS_PATH, text.strip() + "\n")
    return {"ok": True, "count": count}


@app.post("/api/prompts/upload")
async def upload_prompts(file: UploadFile = File(...)) -> dict[str, Any]:
    text = (await file.read()).decode("utf-8")
    parsed = ensure_json_text(text, "prompts")
    try:
        count = len(load_prompts(parsed))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    save_text(PROMPTS_PATH, text.strip() + "\n")
    return {"ok": True, "count": count}


@app.post("/api/workflow/upload")
async def upload_workflow(file: UploadFile = File(...)) -> dict[str, Any]:
    text = (await file.read()).decode("utf-8")
    ensure_json_text(text, "workflow")
    save_text(WORKFLOW_PATH, text.strip() + "\n")
    return {"ok": True}


@app.get("/api/workflow/validate")
def validate_workflow() -> dict[str, Any]:
    try:
        bindings = validate_workflow_config(CONFIG_PATH)
        return {"ok": True, "bindings": bindings}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/storyboard")
async def upload_storyboard(
    file: UploadFile = File(...),
    rows: int = Form(4),
    cols: int = Form(3),
    margin: float = Form(0),
    gutter: float = Form(0),
) -> dict[str, Any]:
    STORYBOARD_PATH.write_bytes(await file.read())
    results = split_storyboard(
        storyboard_path=STORYBOARD_PATH,
        output_dir=CELLS_DIR,
        rows=rows,
        cols=cols,
        margin=margin,
        gutter=gutter,
    )
    return {
        "ok": True,
        "storyboard_path": str(STORYBOARD_PATH),
        "cells": serialize_split_results(results),
    }


@app.post("/api/run")
def start_run(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        validate_workflow_config(CONFIG_PATH)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    options = BatchRunOptions(
        config_path=CONFIG_PATH,
        start_index=int(payload.get("start_index", 1)),
        end_index=int(payload["end_index"]) if payload.get("end_index") not in (None, "") else None,
        overwrite=bool(payload.get("overwrite", False)),
    )
    try:
        batch_manager.start(options)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True}


@app.post("/api/run/stop")
def stop_run() -> dict[str, Any]:
    try:
        stop_state = batch_manager.request_stop()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    config = normalize_config(load_json(CONFIG_PATH))
    base_url = str(config.get("comfyui_base_url", "")).rstrip("/")
    queued_prompt_ids = list(stop_state.get("queued_prompt_ids") or [])
    running_prompt_id = stop_state.get("running_prompt_id")
    warnings: list[str] = []

    if base_url:
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

    return {
        "ok": True,
        "stopping": True,
        "running_prompt_id": running_prompt_id,
        "queued_prompt_ids": queued_prompt_ids,
        "warnings": warnings,
    }


@app.get("/api/run/status")
def run_status() -> dict[str, Any]:
    return {
        "batch_state": batch_manager.snapshot(),
        "outputs": list_outputs(),
        "failed_jobs": load_json(FAILED_JOBS_PATH) if FAILED_JOBS_PATH.exists() else [],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "time": int(time.time())}


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the LTX storyboard batch web app.")
    parser.add_argument(
        "--host",
        default=os.environ.get("LTX_BATCH_HOST", "127.0.0.1"),
        help="Host to bind. Defaults to 127.0.0.1 or LTX_BATCH_HOST.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LTX_BATCH_PORT", "8000")),
        help="Port to bind. Defaults to 8000 or LTX_BATCH_PORT.",
    )
    args = parser.parse_args()

    uvicorn.run("app:app", host=args.host, port=args.port, reload=False)
