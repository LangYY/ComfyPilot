from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
STORE_ROOT = ROOT / "studio_v2_data"
PROBLEM_RUN_STATUSES = {"failed", "interrupted", "unknown", "stopped"}
PROBLEM_TASK_STATUSES = {"failed", "interrupted", "unknown", "cancelled"}


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_time(value: str) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            pass
    return None


def last_jsonl(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not lines:
            return None
        return json.loads(lines[-1])
    except Exception as exc:
        return {"error": f"Could not read {path}: {exc}"}


def compact_preflight(path: Path) -> dict[str, Any] | None:
    payload = load_json(path)
    if not isinstance(payload, dict):
        return None
    task = payload.get("task", {})
    return {
        "created_at": payload.get("created_at"),
        "task": task,
        "run_settings": payload.get("run_settings", {}),
        "workflow_runtime_values": payload.get("workflow_runtime_values", {}),
        "media_paths": payload.get("media_paths", {}),
        "gpu": payload.get("gpu", {}),
        "docker": payload.get("docker", {}),
    }


def collect(store_root: Path, since_days: int) -> dict[str, Any]:
    cutoff = datetime.now() - timedelta(days=since_days)
    reports: list[dict[str, Any]] = []
    for run_path in store_root.glob("projects/*/runs/*/run.json"):
        run = load_json(run_path)
        if not isinstance(run, dict):
            continue
        updated_at = parse_time(str(run.get("updated_at", ""))) or parse_time(str(run.get("created_at", "")))
        if updated_at and updated_at < cutoff:
            continue
        run_status = str(run.get("status", ""))
        tasks = run.get("tasks", [])
        problem_tasks = [
            task for task in tasks
            if str(task.get("status", "")) in PROBLEM_TASK_STATUSES or str(task.get("error", "")).strip()
        ]
        if run_status not in PROBLEM_RUN_STATUSES and not problem_tasks:
            continue

        run_dir = run_path.parent
        task_reports = []
        for task in tasks:
            diag = task.get("diagnostics", {}) if isinstance(task.get("diagnostics"), dict) else {}
            preflight_path = run_dir / str(diag.get("preflight", ""))
            samples_path = run_dir / str(diag.get("samples", ""))
            task_reports.append(
                {
                    "order": task.get("order"),
                    "status": task.get("status"),
                    "prompt_id": task.get("prompt_id"),
                    "error": task.get("error"),
                    "duration_seconds": task.get("duration_seconds"),
                    "expected_output_name": task.get("expected_output_name"),
                    "seed_value": task.get("seed_value"),
                    "preflight": compact_preflight(preflight_path) if diag.get("preflight") else None,
                    "last_sample": last_jsonl(samples_path) if diag.get("samples") else None,
                }
            )

        reports.append(
            {
                "run_path": str(run_path),
                "run_id": run.get("id"),
                "batch_id": run.get("batch_id"),
                "profile_id": run.get("profile_id"),
                "status": run_status,
                "created_at": run.get("created_at"),
                "updated_at": run.get("updated_at"),
                "started_at": run.get("started_at"),
                "ended_at": run.get("ended_at"),
                "run_settings": run.get("run_settings", {}),
                "task_count": len(tasks),
                "problem_task_count": len(problem_tasks),
                "tasks": task_reports,
                "logs_tail": run.get("logs", [])[-12:],
            }
        )
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "store_root": str(store_root),
        "since_days": since_days,
        "run_count": len(reports),
        "runs": sorted(reports, key=lambda item: str(item.get("updated_at", "")), reverse=True),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect ComfyPilot run diagnostics for crash analysis.")
    parser.add_argument("--store-root", default=str(STORE_ROOT), help="Path to studio_v2_data.")
    parser.add_argument("--since-days", type=int, default=14, help="Only include recent runs.")
    parser.add_argument("--output", default="crash_diagnostics_report.json", help="Output JSON path.")
    args = parser.parse_args()

    store_root = Path(args.store_root)
    report = collect(store_root, args.since_days)
    output = Path(args.output)
    if not output.is_absolute():
        output = ROOT / output
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {report['run_count']} run report(s): {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
