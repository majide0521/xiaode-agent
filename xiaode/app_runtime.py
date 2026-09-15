from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Any


def build_subprocess_env(*, run_id: str, output_root: Path, mode: str) -> dict[str, str]:
    allowed = (
        "SILICONFLOW_API_KEY",
        "BOCHA_API_KEY",
        "SILICONFLOW_MODEL",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "LANG",
        "LC_ALL",
    )
    environment = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    environment.update(
        {
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "XIAODE_RUN_ID": run_id,
            "XIAODE_OUTPUT_ROOT": str(output_root),
            "XIAODE_RESEARCH_MODE": mode,
        }
    )
    return environment


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass


def load_manifest(run_dir: Path) -> dict[str, Any]:
    manifest_path = (run_dir / "manifest.json").resolve()
    if manifest_path.parent != run_dir.resolve() or not manifest_path.is_file():
        raise FileNotFoundError("本次运行没有生成 manifest.json")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("manifest.json 格式无效")
    return payload


def artifact_path(run_dir: Path, manifest: dict[str, Any], key: str) -> Path:
    artifacts = manifest.get("artifacts", {})
    if not isinstance(artifacts, dict):
        raise ValueError("manifest.artifacts 格式无效")
    name = artifacts.get(key)
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError(f"artifact {key} 的路径无效")
    path = (run_dir / name).resolve()
    if path.parent != run_dir.resolve() or not path.is_file():
        raise FileNotFoundError(f"找不到本次运行的 artifact：{key}")
    return path
