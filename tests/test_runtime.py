import os
import tempfile
import unittest
from pathlib import Path

from xiaode.app_runtime import artifact_path, build_subprocess_env, load_manifest
from xiaode.runtime import RunWorkspace


class RuntimeIsolationTests(unittest.TestCase):
    def test_each_run_has_its_own_directory_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = RunWorkspace.create(output_root=root, run_id="run_12345678")
            second = RunWorkspace.create(output_root=root, run_id="run_87654321")
            first.write_json("manifest.json", {"status": "complete", "artifacts": {"report": "report.md"}})
            first.write_text("report.md", "first")
            second.write_text("report.md", "second")
            manifest = load_manifest(first.run_dir)
            self.assertEqual(artifact_path(first.run_dir, manifest, "report").read_text(), "first")

    def test_artifact_path_rejects_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary)
            with self.assertRaises(ValueError):
                artifact_path(run_dir, {"artifacts": {"report": "../report.md"}}, "report")

    def test_subprocess_environment_is_allowlisted(self) -> None:
        old_value = os.environ.get("UNRELATED_SECRET")
        os.environ["UNRELATED_SECRET"] = "do-not-copy"
        try:
            env = build_subprocess_env(
                run_id="run_12345678",
                output_root=Path("/tmp/xiaode-tests"),
                mode="quick",
            )
            self.assertNotIn("UNRELATED_SECRET", env)
            self.assertEqual(env["XIAODE_RUN_ID"], "run_12345678")
        finally:
            if old_value is None:
                os.environ.pop("UNRELATED_SECRET", None)
            else:
                os.environ["UNRELATED_SECRET"] = old_value


if __name__ == "__main__":
    unittest.main()
