from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from batch_studio_v2.common import file_url, now_iso
from batch_studio_v2.runner import QueueRunner
from batch_studio_v2.store import StudioStore


ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web_v2"

store = StudioStore(ROOT)
BOOTSTRAP_WARNING = ""
ACCESS_TOKEN = os.environ.get("BATCH_STUDIO_ACCESS_TOKEN", "").strip()
store.get_or_create_default_project()
runner = QueueRunner(store)

COMFYUI_DOCKER_DIR = Path(
    os.environ.get(
        "COMFYUI_DOCKER_DIR",
        r"E:\AI_Projects\ComfyUI-Project\pytorch2.8.0-cu128",
    )
)
COMFYUI_DOCKER_STATE_PATH = COMFYUI_DOCKER_DIR / "comfyui_runtime_profile.json"
SWITCH_COMFYUI_PROFILE_SCRIPT = ROOT / "scripts" / "switch_comfyui_docker_profile.ps1"
DIAGNOSE_COMFYUI_SCRIPT = ROOT / "scripts" / "diagnose_comfyui_docker.ps1"
COMFYUI_RUNTIME_PROFILES = {
    "stable": {
        "label": "稳定优先",
        "description": "低显存模式，预留 6GB 显存，VAE 放 CPU。最稳但更慢，适合长批次无人值守。",
    },
    "balanced": {
        "label": "均衡模式",
        "description": "普通显存模式，预留 3GB 显存。建议作为日常默认。",
    },
    "performance": {
        "label": "性能优先",
        "description": "高显存模式，预留 1GB 显存。最快但最容易 OOM，只建议短批次或低分辨率。",
    },
}

app = FastAPI(title="ComfyPilot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/assets-v2", StaticFiles(directory=WEB_DIR), name="assets-v2")
app.mount("/v2-files", StaticFiles(directory=store.store_root), name="v2-files")


@app.middleware("http")
async def require_access_token(request: Request, call_next):
    if not ACCESS_TOKEN or request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    public_path = path == "/" or path.startswith("/assets-v2")
    if public_path:
        return await call_next(request)

    supplied = request.headers.get("X-Batch-Studio-Token", "")
    if not supplied:
        supplied = request.query_params.get("access_token", "")
    if secrets.compare_digest(str(supplied), ACCESS_TOKEN):
        return await call_next(request)
    return JSONResponse(status_code=401, content={"detail": "Access token required."})


def parse_json_text(text: str, label: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{label} JSON parse failed: {exc}") from exc


def parse_optional_json_text(text: str | None, label: str) -> dict[str, Any]:
    if not text or not text.strip():
        return {}
    value = parse_json_text(text, label)
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail=f"{label} must be a JSON object.")
    return value


def normalize_comfyui_save_subfolder(value: Any) -> str:
    raw = str(value or "").strip().replace("\\", "/").strip("/")
    if not raw:
        return "batch_studio_v2"
    if re.match(r"^[A-Za-z]:", raw) or raw.startswith("/") or ".." in raw.split("/"):
        raise HTTPException(
            status_code=400,
            detail="ComfyUI 保存子目录必须是相对路径，例如 video/batch_studio_v2。若要保存到 E 盘，请先在 ComfyUI 启动参数里配置 output-directory。",
        )
    return raw


def parse_runtime_overrides_text(text: str | None) -> dict[str, Any]:
    payload = parse_optional_json_text(text, "runtime_overrides_json")
    if "save_prefix_root" in payload:
        payload["save_prefix_root"] = normalize_comfyui_save_subfolder(payload.get("save_prefix_root"))
    return payload


def run_powershell_script(script_path: Path, args: list[str] | None = None, *, timeout_seconds: int = 180) -> dict[str, Any]:
    if not script_path.exists():
        raise HTTPException(status_code=500, detail=f"Script not found: {script_path}")
    command = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(script_path),
        *(args or []),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail=f"Command timed out: {' '.join(command)}") from exc
    return {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def command_available(command: list[str], *, timeout_seconds: int = 4) -> tuple[bool, str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout_seconds)
    except Exception as exc:
        return False, str(exc)
    output = (completed.stdout or completed.stderr or "").strip()
    return completed.returncode == 0, output


def read_comfyui_runtime_profile_state() -> dict[str, Any]:
    if not COMFYUI_DOCKER_STATE_PATH.exists():
        return {}
    try:
        return json.loads(COMFYUI_DOCKER_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def task_urls(base_dir: Path, task: dict[str, Any]) -> dict[str, Any]:
    payload = dict(task)
    payload["input_urls"] = []
    for ref in task.get("input_refs", []):
        ref_payload = dict(ref)
        ref_payload["url"] = file_url((base_dir / ref["path"]).resolve().relative_to(store.store_root.resolve()).as_posix())
        payload["input_urls"].append(ref_payload)
    output_path = str(task.get("output_path", "")).strip()
    if output_path:
        payload["output_url"] = file_url((base_dir / output_path).resolve().relative_to(store.store_root.resolve()).as_posix())
    return payload


def summarize_tasks(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "pending": 0,
        "queued": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "cancelled": 0,
        "unknown": 0,
        "interrupted": 0,
    }
    submitted = 0
    current_order = None
    for task in tasks:
        status = str(task.get("status", "pending") or "pending").lower()
        counts[status] = counts.get(status, 0) + 1
        if str(task.get("prompt_id", "")).strip():
            submitted += 1
        if current_order is None and status in {"queued", "running"}:
            current_order = task.get("order")
    return {
        "total": len(tasks),
        "submitted": submitted,
        "current_order": current_order,
        **counts,
    }


def serialize_draft(project_id: str, draft: dict[str, Any]) -> dict[str, Any]:
    draft_dir = store._draft_dir(project_id, draft["id"])
    payload = dict(draft)
    payload["tasks"] = [task_urls(draft_dir, task) for task in draft.get("tasks", [])]
    source_files = dict(draft.get("source_files", {}))
    payload["source_file_urls"] = {
        key: file_url((draft_dir / value).resolve().relative_to(store.store_root.resolve()).as_posix())
        for key, value in source_files.items()
    }
    return payload


def serialize_batch(project_id: str, batch: dict[str, Any]) -> dict[str, Any]:
    batch_dir = store._batch_dir(project_id, batch["id"])
    payload = dict(batch)
    payload["tasks"] = [task_urls(batch_dir, task) for task in batch.get("tasks", [])]
    return payload


def serialize_run(project_id: str, run: dict[str, Any]) -> dict[str, Any]:
    run_dir = store._run_dir(project_id, run["id"])
    payload = dict(run)
    payload["tasks"] = [task_urls(run_dir, task) for task in run.get("tasks", [])]
    payload["task_summary"] = summarize_tasks(payload["tasks"])
    payload["current_task"] = payload["task_summary"]["current_order"]
    return payload


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
            ip = item[4][0]
            if ip and not ip.startswith("127."):
                addresses.add(ip)
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
            if ip and not ip.startswith("127."):
                addresses.add(ip)
    except OSError:
        pass
    return sorted(addresses)


def url_with_host(request: Request, host: str) -> str:
    scheme = request.url.scheme
    port = request.url.port
    port_part = f":{port}" if port else ""
    return f"{scheme}://{host}{port_part}"


def project_detail_payload(project_id: str) -> dict[str, Any]:
    detail = store.project_detail(project_id)
    return {
        "project": detail["project"],
        "profiles": detail["profiles"],
        "drafts": [serialize_draft(project_id, item) for item in detail["drafts"]],
        "batches": [serialize_batch(project_id, item) for item in detail["batches"]],
        "runs": [serialize_run(project_id, item) for item in detail["runs"]],
    }


def selected_task_ids_from_payload(payload: dict[str, Any] | None) -> list[str] | None:
    if not payload:
        return None
    raw_ids = payload.get("selected_task_ids")
    if raw_ids is None:
        return None
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="selected_task_ids must be a list.")
    task_ids = [str(item).strip() for item in raw_ids if str(item).strip()]
    if not task_ids:
        raise HTTPException(status_code=400, detail="Please select at least one task.")
    return task_ids


async def read_text_upload(file: UploadFile | None, label: str) -> str:
    if not file:
        raise HTTPException(status_code=400, detail=f"{label} file is required.")
    raw = await file.read()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"{label} must be UTF-8 text.") from exc


async def read_binary_uploads(files: list[UploadFile] | None, label: str) -> list[tuple[str, bytes]]:
    if not files:
        raise HTTPException(status_code=400, detail=f"{label} files are required.")
    payload: list[tuple[str, bytes]] = []
    for file in files:
        payload.append((file.filename or "image.png", await file.read()))
    return payload


def selected_project_payload() -> dict[str, Any]:
    dashboard = store.dashboard_payload()
    projects = dashboard["projects"]
    selected_project = projects[0] if projects else store.get_or_create_default_project()
    selected_project_id = selected_project["id"]
    return {
        **dashboard,
        "selected_project_id": selected_project_id,
        "selected_project": project_detail_payload(selected_project_id),
        "generated_at": now_iso(),
        "bootstrap_warning": BOOTSTRAP_WARNING,
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/api/dashboard")
def dashboard() -> dict[str, Any]:
    return selected_project_payload()


@app.get("/api/projects/{project_id}")
def project_detail(project_id: str) -> dict[str, Any]:
    try:
        return project_detail_payload(project_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/access-info")
def access_info(request: Request) -> dict[str, Any]:
    lan_ips = local_ipv4_addresses()
    return {
        "current_url": str(request.base_url).rstrip("/"),
        "local_url": url_with_host(request, "127.0.0.1"),
        "lan_urls": [url_with_host(request, ip) for ip in lan_ips],
        "token_enabled": bool(ACCESS_TOKEN),
        "public_mode_hint": "Start with --public or --host 0.0.0.0 to allow LAN/tunnel access.",
        "internet_access_hint": "For access outside home, use a VPN/tunnel or router port forwarding. Keep an access token enabled.",
    }


@app.get("/api/comfyui/runtime-profiles")
def comfyui_runtime_profiles() -> dict[str, Any]:
    project_id = selected_project_payload()["selected_project_id"]
    project = store.load_project(project_id)
    base_url = str(project.get("comfyui", {}).get("base_url", "http://127.0.0.1:8189")).rstrip("/")
    state_payload = read_comfyui_runtime_profile_state()
    docker_ok, docker_message = command_available(["docker", "version"])
    nvidia_ok, nvidia_message = command_available(
        ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"]
    )
    comfyui_ok = False
    comfyui_message = ""
    try:
      response = requests.get(f"{base_url}/queue", timeout=2.5)
      comfyui_ok = response.status_code < 500
      comfyui_message = f"HTTP {response.status_code}"
    except requests.RequestException as exc:
      comfyui_message = str(exc)
    return {
        "profiles": COMFYUI_RUNTIME_PROFILES,
        "current_profile": state_payload.get("profile", "unknown"),
        "state": state_payload,
        "compose_dir": str(COMFYUI_DOCKER_DIR),
        "comfyui_url": base_url,
        "docker_available": docker_ok,
        "docker_message": docker_message,
        "nvidia_available": nvidia_ok,
        "nvidia_smi": nvidia_message,
        "comfyui_reachable": comfyui_ok,
        "comfyui_message": comfyui_message,
    }


@app.post("/api/comfyui/runtime-profiles/apply")
def apply_comfyui_runtime_profile(payload: dict[str, Any]) -> dict[str, Any]:
    profile = str(payload.get("profile", "")).strip().lower()
    if profile not in COMFYUI_RUNTIME_PROFILES:
        raise HTTPException(status_code=400, detail=f"Unknown ComfyUI runtime profile: {profile}")
    force = bool(payload.get("force", False))
    queue_state = store.load_queue_state()
    if not force and (queue_state.get("current") or queue_state.get("queued")):
        raise HTTPException(
            status_code=409,
            detail="Batch queue is not empty. Stop or finish queued work first, or retry with force=true.",
        )
    result = run_powershell_script(
        SWITCH_COMFYUI_PROFILE_SCRIPT,
        ["-Profile", profile, "-ComposeDir", str(COMFYUI_DOCKER_DIR)],
        timeout_seconds=240,
    )
    if result["returncode"] != 0:
        detail = (result.get("stderr") or result.get("stdout") or "Failed to switch ComfyUI profile.").strip()
        raise HTTPException(status_code=400, detail=detail)
    return {
        "ok": True,
        "profile": profile,
        "profile_info": COMFYUI_RUNTIME_PROFILES[profile],
        "result": result,
        "status": comfyui_runtime_profiles(),
    }


@app.post("/api/comfyui/runtime-profiles/diagnose")
def diagnose_comfyui_runtime_profile() -> dict[str, Any]:
    result = run_powershell_script(
        DIAGNOSE_COMFYUI_SCRIPT,
        ["-ComposeDir", str(COMFYUI_DOCKER_DIR)],
        timeout_seconds=90,
    )
    return {"ok": result["returncode"] == 0, "result": result}


@app.post("/api/projects")
def create_project(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        project = store.create_project(
            name=str(payload.get("name", "Untitled Project")),
            comfyui_base_url=str(payload.get("comfyui_base_url", "http://127.0.0.1:8188")),
            comfyui_output_dir=str(payload.get("comfyui_output_dir", "")),
        )
        return {"ok": True, "project": project}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/settings")
def update_settings(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        project_id = selected_project_payload()["selected_project_id"]
        current_project = store.load_project(project_id)
        final_output_dir = str(payload.get("final_output_dir", "")).strip()
        comfyui_output_dir = str(
            payload.get("comfyui_output_dir", current_project.get("comfyui", {}).get("output_dir", ""))
        ).strip()
        save_prefix_root = normalize_comfyui_save_subfolder(payload.get("save_prefix_root"))
        project = store.update_project_settings(
            project_id,
            comfyui_base_url=str(payload.get("comfyui_base_url", "http://127.0.0.1:8189")),
            comfyui_output_dir=comfyui_output_dir,
            default_run_settings={
                "final_output_dir": final_output_dir,
                "save_prefix_root": save_prefix_root,
                "output_name_prefix": str(payload.get("output_name_prefix", "")),
                "width_pixels": payload.get("width_pixels"),
                "height_pixels": payload.get("height_pixels"),
                "duration_seconds": payload.get("duration_seconds"),
                "seed_mode": str(payload.get("seed_mode", "fixed")),
                "seed_fixed": int(payload.get("seed_fixed") or 1),
                "seed_base": int(payload.get("seed_fixed") or 1),
                "negative_prompt_text": str(payload.get("negative_prompt_text", "")),
            },
        )
        return {"ok": True, "project": project}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/profiles/upload")
async def upload_profile_workflow(
    project_id: str,
    file: UploadFile = File(...),
    name: str = Form("Workflow Profile"),
    config_hint_json: str = Form(""),
) -> dict[str, Any]:
    try:
        workflow_text = await read_text_upload(file, "workflow")
        profile = store.create_profile_from_text(
            project_id=project_id,
            name=name,
            workflow_text=workflow_text,
            config_hint=parse_optional_json_text(config_hint_json, "config_hint_json"),
        )
        return {"ok": True, "profile": profile}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/drafts/prompt-only")
async def create_prompt_only_draft(
    project_id: str,
    profile_id: str = Form(...),
    prompts_text: str = Form(""),
    prompts_file: UploadFile | None = File(None),
    runtime_overrides_json: str = Form(""),
) -> dict[str, Any]:
    try:
        if prompts_file:
            prompts_text = await read_text_upload(prompts_file, "prompts")
        if not prompts_text.strip():
            raise HTTPException(status_code=400, detail="prompts text or file is required.")
        draft = store.create_prompt_only_draft(
            project_id=project_id,
            profile_id=profile_id,
            prompts_text=prompts_text,
            runtime_overrides=parse_runtime_overrides_text(runtime_overrides_json),
        )
        return {"ok": True, "draft": serialize_draft(project_id, draft)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/drafts/storyboard")
async def create_storyboard_draft(
    project_id: str,
    profile_id: str = Form(...),
    prompts_text: str = Form(""),
    prompts_file: UploadFile | None = File(None),
    storyboard_file: UploadFile = File(...),
    rows: int = Form(4),
    cols: int = Form(3),
    cell_count: int | None = Form(None),
    margin: float = Form(0),
    gutter: float = Form(0),
    runtime_overrides_json: str = Form(""),
) -> dict[str, Any]:
    try:
        if prompts_file:
            prompts_text = await read_text_upload(prompts_file, "prompts")
        if not prompts_text.strip():
            raise HTTPException(status_code=400, detail="prompts text or file is required.")
        storyboard_bytes = await storyboard_file.read()
        draft = store.create_storyboard_draft(
            project_id=project_id,
            profile_id=profile_id,
            prompts_text=prompts_text,
            storyboard_name=storyboard_file.filename or "storyboard.png",
            storyboard_bytes=storyboard_bytes,
            rows=rows,
            cols=cols,
            cell_count=cell_count,
            margin=margin,
            gutter=gutter,
            runtime_overrides=parse_runtime_overrides_text(runtime_overrides_json),
        )
        return {"ok": True, "draft": serialize_draft(project_id, draft)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/drafts/image-batch")
async def create_image_batch_draft(
    project_id: str,
    profile_id: str = Form(...),
    prompts_text: str = Form(""),
    prompts_file: UploadFile | None = File(None),
    image_files: list[UploadFile] | None = File(None),
    runtime_overrides_json: str = Form(""),
) -> dict[str, Any]:
    try:
        if prompts_file:
            prompts_text = await read_text_upload(prompts_file, "prompts")
        if not prompts_text.strip():
            raise HTTPException(status_code=400, detail="prompts text or file is required.")
        draft = store.create_image_batch_draft(
            project_id=project_id,
            profile_id=profile_id,
            prompts_text=prompts_text,
            image_files=await read_binary_uploads(image_files, "first-frame image"),
            runtime_overrides=parse_runtime_overrides_text(runtime_overrides_json),
        )
        return {"ok": True, "draft": serialize_draft(project_id, draft)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/drafts/first-last")
async def create_first_last_draft(
    project_id: str,
    profile_id: str = Form(...),
    prompts_text: str = Form(""),
    prompts_file: UploadFile | None = File(None),
    first_files: list[UploadFile] | None = File(None),
    last_files: list[UploadFile] | None = File(None),
    continuous_pairs: bool = Form(False),
    runtime_overrides_json: str = Form(""),
) -> dict[str, Any]:
    try:
        if prompts_file:
            prompts_text = await read_text_upload(prompts_file, "prompts")
        if not prompts_text.strip():
            raise HTTPException(status_code=400, detail="prompts text or file is required.")
        draft = store.create_first_last_draft(
            project_id=project_id,
            profile_id=profile_id,
            prompts_text=prompts_text,
            first_files=await read_binary_uploads(first_files, "first-frame image"),
            last_files=await read_binary_uploads(last_files, "last-frame image") if not continuous_pairs else None,
            continuous_pairs=bool(continuous_pairs),
            runtime_overrides=parse_runtime_overrides_text(runtime_overrides_json),
        )
        return {"ok": True, "draft": serialize_draft(project_id, draft)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/drafts/{draft_id}/submit")
def submit_draft(project_id: str, draft_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        batch = store.freeze_draft_to_batch(
            project_id,
            draft_id,
            status="queued",
            selected_task_ids=selected_task_ids_from_payload(payload),
        )
        run = store.create_run_from_batch(project_id, batch["id"], reason="new")
        runner.enqueue_run(project_id, run["id"])
        return {
            "ok": True,
            "batch": serialize_batch(project_id, batch),
            "run": serialize_run(project_id, run),
        }
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/drafts/{draft_id}/plan")
def plan_draft(project_id: str, draft_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        batch = store.freeze_draft_to_batch(
            project_id,
            draft_id,
            status="planned",
            selected_task_ids=selected_task_ids_from_payload(payload),
        )
        return {"ok": True, "batch": serialize_batch(project_id, batch)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/batches/{batch_id}/run")
def run_planned_batch(project_id: str, batch_id: str) -> dict[str, Any]:
    try:
        run = store.create_run_from_batch(project_id, batch_id, reason="planned")
        runner.enqueue_run(project_id, run["id"])
        return {"ok": True, "run": serialize_run(project_id, run)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/batches/{batch_id}/schedule")
def schedule_planned_batch(project_id: str, batch_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        run_at = str(payload.get("run_at", "")).strip()
        if not run_at:
            raise HTTPException(status_code=400, detail="run_at is required.")
        batch = store.schedule_batch(project_id, batch_id, run_at)
        runner.wake()
        return {"ok": True, "batch": serialize_batch(project_id, batch)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/projects/{project_id}/batches/{batch_id}")
def delete_planned_batch(project_id: str, batch_id: str) -> dict[str, Any]:
    try:
        result = store.delete_planned_batch(project_id, batch_id)
        return {"ok": True, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/runs/{run_id}/stop")
def stop_run(project_id: str, run_id: str) -> dict[str, Any]:
    try:
        result = runner.request_stop(project_id, run_id)
        return {"ok": True, **result}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/runs/stop-all")
def stop_all_runs() -> dict[str, Any]:
    try:
        return {"ok": True, **runner.request_stop_all()}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/runs/{run_id}/retry")
def retry_run(project_id: str, run_id: str) -> dict[str, Any]:
    try:
        run = store.retry_run(project_id, run_id)
        runner.enqueue_run(project_id, run["id"])
        return {"ok": True, "run": serialize_run(project_id, run)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/projects/{project_id}/runs/{run_id}/tasks/{task_id}/retry")
def retry_run_task(project_id: str, run_id: str, task_id: str) -> dict[str, Any]:
    try:
        run = store.retry_run_task(project_id, run_id, task_id)
        runner.enqueue_run(project_id, run["id"])
        return {"ok": True, "run": serialize_run(project_id, run)}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "time": now_iso(),
        "queue_state": store.load_queue_state(),
        "runner": {
            "enabled": runner.enabled,
            "runner_id": runner.runner_id,
            "thread_alive": bool(runner._thread and runner._thread.is_alive()),
            "last_heartbeat": runner.last_heartbeat,
            "last_error": runner.last_error,
        },
    }


if __name__ == "__main__":
    import uvicorn

    parser = argparse.ArgumentParser(description="Run ComfyPilot.")
    parser.add_argument(
        "--host",
        default=os.environ.get("BATCH_STUDIO_V2_HOST", "127.0.0.1"),
        help="Host to bind. Defaults to 127.0.0.1 or BATCH_STUDIO_V2_HOST.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BATCH_STUDIO_V2_PORT", "8002")),
        help="Port to bind. Defaults to 8002 or BATCH_STUDIO_V2_PORT.",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Bind to 0.0.0.0 so other devices or tunnels can reach this app.",
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("BATCH_STUDIO_ACCESS_TOKEN", ""),
        help="Optional access token required for API and file endpoints.",
    )
    args = parser.parse_args()
    if args.public:
        args.host = "0.0.0.0"
    ACCESS_TOKEN = str(args.access_token or "").strip()
    if args.host in {"0.0.0.0", "::"} and not ACCESS_TOKEN:
        print("[WARN] Public bind is enabled without an access token. Use --access-token or BATCH_STUDIO_ACCESS_TOKEN.")

    uvicorn.run(app, host=args.host, port=args.port, reload=False)
