import tempfile
import unittest
from pathlib import Path

from xiaode.evals import evaluate_runs
from xiaode.runtime import RunWorkspace


class EvalAggregationTests(unittest.TestCase):
    def test_aggregates_complete_and_failed_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete = RunWorkspace.create(output_root=root, run_id="evalrun_12345678")
            complete.write_json(
                "manifest.json",
                {
                    "status": "complete",
                    "duration_seconds": 12,
                    "counts": {
                        "sources": 5,
                        "evidence": 4,
                        "approved": 3,
                        "caution": 1,
                        "rejected": 0,
                    },
                    "quality": {"overall": "MEDIUM", "report_validation": "PASS"},
                    "usage": {"total_tokens": 250},
                },
            )
            complete.write_json("evidence_precheck.json", {"E02": ["quote mismatch"]})
            failed = RunWorkspace.create(output_root=root, run_id="evalrun_87654321")
            failed.write_json("manifest.json", {"status": "failed"})

            result = evaluate_runs(root)
            self.assertEqual(result["runs_total"], 2)
            self.assertEqual(result["runs_complete"], 1)
            self.assertEqual(result["runs_failed"], 1)
            self.assertEqual(result["report_hard_gate_pass_rate"], 1.0)
            self.assertEqual(result["precheck_errors"], 1)
            self.assertEqual(result["average_sources"], 5.0)
            self.assertEqual(result["average_total_tokens"], 250.0)
