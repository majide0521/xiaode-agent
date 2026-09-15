from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import APP_VERSION


_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{8,80}$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def safe_artifact_name(value: str) -> str:
    name = re.sub(r"[^\w\-]+", "_", value, flags=re.UNICODE).strip("_")
    return (name or "competitor")[:80]


@dataclass(frozen=True)
class RunWorkspace:
    run_id: str
    root: Path
    run_dir: Path
    candidates_path: Path
    events_path: Path

    @classmethod
    def create(
        cls,
        *,
        output_root: str | Path | None = None,
        run_id: str | None = None,
    ) -> "RunWorkspace":
        chosen_id = (run_id or uuid.uuid4().hex).strip()
        if not _RUN_ID.fullmatch(chosen_id):
            raise ValueError("run_id 只能包含字母、数字、下划线和连字符")
        root = Path(output_root or os.getenv("XIAODE_OUTPUT_ROOT", "runs")).expanduser().resolve()
        run_dir = (root / chosen_id).resolve()
        if run_dir.parent != root:
            raise ValueError("run_id 不能跳出输出目录")
        run_dir.mkdir(parents=True, exist_ok=False)
        workspace = cls(
            run_id=chosen_id,
            root=root,
            run_dir=run_dir,
            candidates_path=run_dir / "candidates.jsonl",
            events_path=run_dir / "events.jsonl",
        )
        workspace.log_event("run_created", version=APP_VERSION)
        return workspace

    def path(self, name: str) -> Path:
        if Path(name).name != name:
            raise ValueError("artifact name must be a file name")
        return self.run_dir / name

    def write_text(self, name: str, text: str) -> Path:
        path = self.path(name)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
        return path

    def write_json(self, name: str, payload: Any) -> Path:
        return self.write_text(
            name,
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        )

    def append_jsonl(self, name: str, payload: Any) -> None:
        path = self.path(name)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def log_event(self, event: str, **fields: Any) -> None:
        self.append_jsonl(
            self.events_path.name,
            {"at": utc_now(), "event": event, **fields},
        )

    def write_failure_manifest(self, error: BaseException) -> None:
        self.write_json(
            "manifest.json",
            {
                "version": APP_VERSION,
                "run_id": self.run_id,
                "status": "failed",
                "finished_at": utc_now(),
                "error_type": type(error).__name__,
                "error": str(error)[:1000],
            },
        )
