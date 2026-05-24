from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slugify(value: str, *, default: str = "item") -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or default


def make_id(prefix: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}_{uuid.uuid4().hex[:6]}"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    tmp_path.replace(path)


def load_text(path: Path, default: str = "") -> str:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as handle:
        return handle.read()


def save_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    tmp_path.replace(path)


def deep_copy_jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False))


def copy_file(source: Path, destination: Path) -> Path:
    ensure_dir(destination.parent)
    shutil.copy2(source, destination)
    return destination


def copy_tree(source: Path, destination: Path) -> Path:
    ensure_dir(destination.parent)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    return destination


def to_relative_string(base_dir: Path, path: Path) -> str:
    return path.resolve().relative_to(base_dir.resolve()).as_posix()


def file_url(relative_path: str) -> str:
    normalized = relative_path.replace("\\", "/").lstrip("/")
    return f"/v2-files/{normalized}"
