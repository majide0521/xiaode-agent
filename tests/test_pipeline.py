import tempfile
import unittest
from pathlib import Path
from typing import Any

from xiaode.models import AuditDecision, AuditReport, EvidenceItem, EvidenceLedger, SearchPlan, SourceRecord
from xiaode.pipeline import PipelineConfig, ResearchPipeline
from xiaode.runtime import RunWorkspace


class FakeRetrieval:
    def __init__(self, source: SourceRecord) -> None:
        self.sources = [source]
        self.executed_queries = {"query"}

    def discover_official(self) -> None:
        return None

    def execute_plan(self, _plan: SearchPlan) -> None:
        return None

    def fetch_sources(self) -> list[SourceRecord]:
        return self.sources

    def repair_coverage(self) -> dict[str, dict[str, Any]]:
        return {"product": {"label": "产品与能力", "source_ids": ["S01"], "covered": True}}


class OfflinePipeline(ResearchPipeline):
    def __init__(self, workspace: RunWorkspace) -> None:
        self.competitor = "Example"
        self.official_domain = "example.com"
        self.focus = "商业模式和定价"
        self.config = PipelineConfig("dummy", "dummy", "dummy-model", "quick")
        self.workspace = workspace
        self.usage = {"requests": 4, "input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        source = SourceRecord(
            source_id="S01",
            title="Official pricing",
            url="https://example.com/pricing",
            domain="example.com",
            source_tier="Tier A",
            published_date="2026-09-01",
            quality_score=120,
            content="The Pro plan costs $20 per month and is available to individual subscribers.",
        )
        self.retrieval = FakeRetrieval(source)
        ledger = EvidenceLedger(
            evidence=[
                EvidenceItem(
                    evidence_id="E01",
                    claim="The Pro plan costs $20 per month.",
                    claim_nature="FACT",
                    source_id="S01",
                    source_name="Official pricing",
                    source_tier="Tier A",
                    url="https://example.com/pricing",
                    published_date="2026-09-01",
                    evidence_text="The Pro plan costs $20 per month",
                    evidence_type="价格",
                    confidence="High",
                    atomicity="PASS",
                    entailment="PASS",
                    reason="The quote states the price.",
                )
            ],
            missing_evidence=[],
        )
        audit = AuditReport(
            decisions=[
                AuditDecision(
                    evidence_id="E01",
                    status="APPROVED",
                    approved_claim="The Pro plan costs $20 per month.",
                    claim_nature="FACT",
                    source_id="S01",
                    source_tier="Tier A",
                    confidence="High",
                    required_qualification="",
                    audit_reason="The quote directly supports the claim.",
                )
            ],
            risks=[],
        )
        headings = [
            "# 竞品研究报告",
            "## 0. Executive Summary",
            "## 1. 一句话判断",
            "## 2. 产品定位",
            "## 3. 核心用户与场景",
            "## 4. 核心产品能力",
            "## 5. 最近半年产品变化",
            "## 6. 用户与市场表现",
            "## 7. 商业模式",
            "## 8. 增长逻辑",
            "## 9. 核心竞争壁垒",
            "## 10. 主要问题与风险",
            "## 11. 对用户指定业务的威胁",
            "## 12. 值得借鉴的策略",
            "## 13. 最终判断",
            "【事实】The Pro plan costs $20 per month. [E01]",
            "【判断】This supports a paid subscription positioning. [E01]",
            "## 14. 研究缺口",
            "【研究缺口】缺少用户留存数据。",
            "## 15. Sources",
            "- [E01] https://example.com/pricing",
        ]
        self.outputs: list[Any] = [
            SearchPlan(
                queries=[
                    {"query": "Example official product", "dimension": "product", "preferred_source": "official"},
                    {"query": "Example pricing", "dimension": "business", "preferred_source": "official"},
                    {"query": "Example market", "dimension": "users_market", "preferred_source": "authority"},
                ]
            ),
            ledger,
            audit,
            "\n\n".join(headings),
        ]

    def _planner(self) -> Any:
        return object()

    def _extractor(self) -> Any:
        return object()

    def _auditor(self) -> Any:
        return object()

    def _writer(self) -> Any:
        return object()

    def _run_agent(self, _agent: Any, _task: str, *, max_turns: int = 3) -> Any:
        return self.outputs.pop(0)


class PipelineIntegrationTests(unittest.TestCase):
    def test_offline_pipeline_writes_verified_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = RunWorkspace.create(
                output_root=Path(temporary),
                run_id="pipeline_12345678",
            )
            manifest = OfflinePipeline(workspace).run()
            self.assertEqual(manifest["status"], "complete")
            self.assertEqual(manifest["quality"]["report_validation"], "PASS")
            self.assertTrue((workspace.run_dir / "report.md").is_file())
            self.assertTrue((workspace.run_dir / "audit.json").is_file())


if __name__ == "__main__":
    unittest.main()
