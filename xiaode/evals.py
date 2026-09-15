from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate_runs(runs_root: str | Path) -> dict[str, Any]:
    root = Path(runs_root)
    manifests = sorted(root.glob("*/manifest.json")) if root.exists() else []
    completed: list[dict[str, Any]] = []
    failed = 0
    precheck_error_count = 0
    report_passes = 0
    durations: list[float] = []
    quality_distribution: Counter[str] = Counter()

    for manifest_path in manifests:
        try:
            manifest = _read_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            failed += 1
            continue
        if manifest.get("status") != "complete":
            failed += 1
            continue
        completed.append(manifest)
        duration = manifest.get("duration_seconds")
        if isinstance(duration, (int, float)):
            durations.append(float(duration))
        quality = manifest.get("quality", {})
        quality_distribution[str(quality.get("overall", "UNKNOWN"))] += 1
        if quality.get("report_validation") == "PASS":
            report_passes += 1
        precheck_path = manifest_path.parent / "evidence_precheck.json"
        if precheck_path.is_file():
            try:
                precheck = _read_json(precheck_path)
                if isinstance(precheck, dict):
                    precheck_error_count += sum(
                        len(errors) for errors in precheck.values() if isinstance(errors, list)
                    )
            except (OSError, json.JSONDecodeError):
                precheck_error_count += 1

    total = len(manifests)
    complete_count = len(completed)

    def average_count(key: str) -> float:
        values = [
            manifest.get("counts", {}).get(key)
            for manifest in completed
            if isinstance(manifest.get("counts", {}).get(key), (int, float))
        ]
        return round(mean(values), 2) if values else 0.0

    token_values = [
        manifest.get("usage", {}).get("total_tokens")
        for manifest in completed
        if isinstance(manifest.get("usage", {}).get("total_tokens"), (int, float))
    ]

    return {
        "runs_total": total,
        "runs_complete": complete_count,
        "runs_failed": failed,
        "completion_rate": round(complete_count / total, 4) if total else 0.0,
        "report_hard_gate_pass_rate": round(report_passes / complete_count, 4) if complete_count else 0.0,
        "precheck_errors": precheck_error_count,
        "average_duration_seconds": round(mean(durations), 2) if durations else 0.0,
        "average_sources": average_count("sources"),
        "average_evidence": average_count("evidence"),
        "average_approved": average_count("approved"),
        "average_caution": average_count("caution"),
        "average_rejected": average_count("rejected"),
        "average_total_tokens": round(mean(token_values), 2) if token_values else 0.0,
        "quality_distribution": dict(sorted(quality_distribution.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate Xiaode run quality metrics")
    parser.add_argument("--runs-root", default="runs")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = evaluate_runs(args.runs_root)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
